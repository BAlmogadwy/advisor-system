"""Check every rule record's `source_text_ar` against the PDF's own text layer.

A record that claims to quote a page must actually quote it. Two independent LLM
extractions of this guide both failed that test — silently repairing the source's
typos, dropping words, and normalising Arabic-Indic numerals to ASCII. This script
is the mechanical check that catches it.

Reports per record:
  EXACT      — the excerpt appears in the page text verbatim after normalisation
  NEAR       — >= 0.90 token recall; likely a small transcription slip
  DIVERGENT  — 0.60-0.90 recall; wording differs materially
  UNSUPPORTED— < 0.60 recall; the page does not say this
  NO_EXCERPT — record has no source_text_ar (structural records: tables, comparisons)

Normalisation is deliberately narrow — it folds Arabic-Indic to ASCII digits,
strips diacritics/tatweel, and unifies alef/ya/ta-marbuta forms, because the text
layer and a human transcription legitimately differ there. It does NOT fold word
boundaries or repair spelling, because those are exactly the errors being hunted.

Exit code 1 if any record is DIVERGENT or UNSUPPORTED.

Usage:  python policies/tools/check_excerpts.py [--verbose]
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE_TEXT = ROOT / "evidence" / "page_text"

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٪", "0123456789.%")
_NON_WORD = re.compile(r"[^\w؀-ۿ]+")
# Arabic punctuation lives INSIDE the Arabic block, so a naive "\w or Arabic" word
# class treats ، ؛ ؟ as letters and every clause separator becomes a phantom token
# difference. Strip it explicitly before tokenising.
_AR_PUNCT = re.compile(r"[،؛؟٬۔٭﴾﴿]")


def normalise(text: str) -> str:
    text = text.translate(_AR_DIGITS)
    text = _AR_PUNCT.sub(" ", text)
    text = _DIACRITICS.sub("", text)
    text = re.sub(r"[أإآٱ]", "ا", text)
    text = re.sub(r"[ىئ]", "ي", text)
    text = re.sub(r"ة", "ه", text)
    text = re.sub(r"ؤ", "و", text)
    return _NON_WORD.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalise(text).split() if t]


def load_pages() -> dict[int, str]:
    pages: dict[int, str] = {}
    for f in sorted(PAGE_TEXT.glob("page_*.txt")):
        pages[int(f.stem.split("_")[1])] = normalise(f.read_text(encoding="utf-8"))
    if not pages:
        sys.exit(f"no page text found in {PAGE_TEXT} — run extract_page_text.py first")
    return pages


def record_pages(rec: dict) -> list[int]:
    src = rec.get("source") or {}
    if src.get("page") is not None:
        return [int(src["page"])]
    return [int(p) for p in (src.get("pages") or [])]


def classify(excerpt: str, page_blob: str) -> tuple[str, float, list[str]]:
    norm = normalise(excerpt)
    if norm and norm in page_blob:
        return "EXACT", 1.0, []
    toks = tokens(excerpt)
    if not toks:
        return "NO_EXCERPT", 0.0, []
    page_tokens = set(page_blob.split())
    missing = [t for t in toks if t not in page_tokens]
    recall = 1.0 - (len(missing) / len(toks))
    if recall >= 0.90:
        band = "NEAR"
    elif recall >= 0.60:
        band = "DIVERGENT"
    else:
        band = "UNSUPPORTED"
    return band, recall, missing


def main() -> int:
    verbose = "--verbose" in sys.argv
    pages = load_pages()

    results: list[tuple[str, str, float, list[str], list[int]]] = []
    for path in sorted(ROOT.rglob("*.yaml")):
        if path.name == "sources.yaml" or "evidence" in path.parts or "tools" in path.parts:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        for rec in data:
            pid = rec.get("policy_id", "?")
            pgs = record_pages(rec)
            excerpt = rec.get("source_text_ar")
            if not excerpt:
                results.append((pid, "NO_EXCERPT", 0.0, [], pgs))
                continue
            blob = " ".join(pages.get(p, "") for p in pgs)
            if not blob:
                results.append((pid, "NO_PAGE_TEXT", 0.0, [], pgs))
                continue
            band, recall, missing = classify(excerpt, blob)
            results.append((pid, band, recall, missing, pgs))

    order = ["UNSUPPORTED", "DIVERGENT", "NEAR", "EXACT", "NO_EXCERPT", "NO_PAGE_TEXT"]
    counts = {b: sum(1 for r in results if r[1] == b) for b in order}
    print("excerpt fidelity vs PDF text layer")
    for b in order:
        if counts[b]:
            print(f"  {b:<13} {counts[b]:>3}")

    bad = [r for r in results if r[1] in ("UNSUPPORTED", "DIVERGENT")]
    near = [r for r in results if r[1] == "NEAR"]

    for label, rows in (("FAIL", bad), ("NEAR (review)", near if verbose else [])):
        for pid, band, recall, missing, pgs in sorted(rows, key=lambda r: r[2]):
            print(f"\n  [{label}] {pid}  p{pgs}  {band} recall={recall:.2f}")
            print(f"    words not on the page: {' '.join(missing[:12])}")

    print(f"\n{len(bad)} record(s) fail; {len(near)} near-miss")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
