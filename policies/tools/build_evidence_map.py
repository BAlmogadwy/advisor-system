"""Link each rule record to the evidence records that back it.

Written to a SEPARATE generated file rather than into the rule YAML on purpose:

  * the mapping is DERIVED, so it should be regenerable, not hand-maintained;
  * writing it back would destroy the rule files' comments and YAML anchors;
  * when either layer changes, you regenerate and diff the map — which is exactly
    the drift detection that a bare `derived_from: [id]` list cannot give you.

Each link carries a declared relationship, because the two layers are NOT
paraphrases of each other and pretending otherwise is how a citation ends up
pointing at text that does not contain the claim:

  VERBATIM_MATCH    the rule's excerpt is contained in the evidence text
  A_IS_SUPERSET     the rule's excerpt covers this evidence and more
  DIVERGENT_WORDING same page, same subject, materially different wording
  STRUCTURE_ONLY    the rule is a table/comparison/definitions block; the evidence
                    records are its individual cells and cannot reconstruct it

Links also carry the evidence record's content_sha256, so a drifted evidence
record fails validation loudly instead of silently re-backing a rule.

Usage:  python policies/tools/build_evidence_map.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "knowledge_records.yaml"
OUT = ROOT / "evidence_map.yaml"

from arabic_text import content_tokens as toks  # noqa: E402
from arabic_text import normalise as norm  # noqa: E402

# Rule shapes whose content is 2-D. The evidence layer atomises these into
# independent assertions, which cannot reconstruct the table or the contrast.
STRUCTURAL = {"lookup_table", "comparison", "definitions", "enumeration"}


def pages_of(rec: dict) -> list[int]:
    src = rec.get("source") or {}
    if src.get("page") is not None:
        return [int(src["page"])]
    return [int(p) for p in (src.get("pages") or [])]


def rule_text(rec: dict) -> str:
    """Everything in the rule that carries meaning, for matching purposes."""
    parts = [rec.get("source_text_ar") or "", rec.get("title_ar") or ""]
    for key in ("terms", "rights", "duties", "tools", "penalties", "symbols", "scale", "bands"):
        val = rec.get(key)
        if isinstance(val, list):
            for item in val:
                parts.append(
                    " ".join(str(v) for v in item.values()) if isinstance(item, dict) else str(item)
                )
    return " ".join(parts)


def main() -> int:
    if not EVIDENCE.exists():
        sys.exit(f"evidence layer not found: {EVIDENCE} — run import_evidence.py first")

    ev = yaml.safe_load(EVIDENCE.read_text(encoding="utf-8"))["records"]
    by_page: dict[int, list[dict]] = {}
    for e in ev:
        by_page.setdefault(int(e["source_page"]), []).append(e)

    mapping: dict[str, dict] = {}
    stats = {"VERBATIM_MATCH": 0, "A_IS_SUPERSET": 0, "DIVERGENT_WORDING": 0, "STRUCTURE_ONLY": 0}
    unbacked: list[str] = []

    for path in sorted(ROOT.rglob("*.yaml")):
        if (
            path.name in ("sources.yaml", "evidence_map.yaml")
            or "evidence" in path.parts
            or "tools" in path.parts
        ):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue

        for rec in data:
            pid = rec["policy_id"]
            pgs = pages_of(rec)
            structural = rec.get("rule_type") in STRUCTURAL
            rtoks = toks(rule_text(rec))
            excerpt_norm = norm(rec.get("source_text_ar") or "")

            links = []
            for page in pgs:
                for e in by_page.get(page, []):
                    etoks = toks(f"{e.get('statement_ar', '')} {e.get('title_ar', '')}")
                    if not etoks:
                        continue
                    overlap = len(rtoks & etoks) / len(etoks)
                    if overlap < 0.45:
                        continue
                    if structural:
                        rel = "STRUCTURE_ONLY"
                    elif excerpt_norm and norm(e.get("statement_ar", "")) in excerpt_norm:
                        rel = "VERBATIM_MATCH"
                    elif overlap >= 0.80:
                        rel = "A_IS_SUPERSET"
                    else:
                        rel = "DIVERGENT_WORDING"
                    links.append(
                        {
                            "id": e["record_id"],
                            "page": page,
                            "relationship": rel,
                            "coverage": round(overlap, 2),
                            "content_sha256": e["content_sha256"],
                            **({"contested": True} if e.get("contested") else {}),
                            **(
                                {}
                                if e.get("second_pass_checked")
                                else {"second_pass_checked": False}
                            ),
                        }
                    )

            links.sort(key=lambda x: (-x["coverage"], x["id"]))
            for link in links:
                stats[link["relationship"]] = stats.get(link["relationship"], 0) + 1
            if not links:
                unbacked.append(pid)

            entry: dict = {"pages": pgs, "derived_from": links}
            if structural:
                entry["note"] = (
                    "STRUCTURE_ONLY: the evidence records are individual cells. They are "
                    "provenance for the values, not a substitute for the table — the "
                    "2-D relationship between cells exists only on the rendered page."
                )
            if rec.get("rule_type") == "comparison":
                entry["source_silence_cells"] = (
                    "Cells asserting not_stated are claims about what the page does NOT "
                    "say. No evidence record can back an absence; they are verifiable only "
                    "against evidence/page_text/ plus the rendered page."
                )
            mapping[pid] = entry

    payload = {
        "generated_by": "policies/tools/build_evidence_map.py",
        "generated_at": "2026-08-01",
        "regenerate_when": "either layer changes; diff the result to detect evidence drift",
        "relationship_vocabulary": {
            "VERBATIM_MATCH": "the rule's excerpt contains the evidence statement",
            "A_IS_SUPERSET": "the rule's excerpt covers this evidence and more",
            "DIVERGENT_WORDING": "same page and subject, materially different wording",
            "STRUCTURE_ONLY": "rule is a table/comparison/definitions block; evidence are its cells",
        },
        "warning": (
            "A link is NOT a claim that the evidence text quotes the page. The evidence "
            "layer's restatement_ar is a paraphrase (is_verbatim: false). For verbatim "
            "wording cite evidence/page_text/page_NN.txt."
        ),
        "stats": {**stats, "rules_mapped": len(mapping), "rules_unbacked": len(unbacked)},
        "unbacked_rules": unbacked,
        "map": mapping,
    }
    OUT.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    print(f"mapped {len(mapping)} rules -> {OUT}")
    for k, v in stats.items():
        print(f"  {k:<18} {v:>4} links")
    print(f"  unbacked rules:    {len(unbacked)} {unbacked or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
