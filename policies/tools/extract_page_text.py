"""Extract the PDF's own text layer, one file per printed page.

This exists because a source-verification pass needs something to verify AGAINST.
Both LLM extractions of this guide turned out to paraphrase while claiming to quote:
extraction B's `source_excerpt_ar` is byte-identical to its own restatement in
273/277 records, and extraction A's `source_text_ar` was caught silently repairing
the source's own typo (المدة الازمة -> اللازمة) and dropping words (الرئيس).

Neither is verbatim. This is. It is machine-extracted with no model in the loop,
so it is the only artefact in the store that can serve as ground truth for the
human SOURCE_VERIFIED pass.

Caveat recorded rather than hidden: a PDF text layer is the *encoded* text, which
for Arabic can differ from the rendered glyphs in ligature and shaping decisions,
and it carries no table structure. It is ground truth for WORDING, not for LAYOUT.
Tables still have to be read from the rendered page.

Usage:  python policies/tools/extract_page_text.py
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    sys.exit("pypdf is required: pip install pypdf")

ROOT = pathlib.Path(__file__).resolve().parents[1]
PDF = ROOT / "sources" / "TU_STUDENT_GUIDE_V3_1447.pdf"
OUT = ROOT / "evidence" / "page_text"

EXPECTED_SHA256 = "155b7df5ec782860cc047287919e2dcfbe78aa76759b9f0bed105bcad0a15227"


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    if not PDF.exists():
        sys.exit(f"source PDF not found: {PDF}")

    actual = sha256(PDF)
    if actual != EXPECTED_SHA256:
        sys.exit(
            f"source PDF hash mismatch\n  expected {EXPECTED_SHA256}\n  actual   {actual}\n"
            "Refusing to extract: the registry identifies the source by hash."
        )

    OUT.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(PDF))

    index: list[dict[str, object]] = []
    empty: list[int] = []

    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        normalised = "\n".join(line.rstrip() for line in text.splitlines()).strip()
        target = OUT / f"page_{i:02d}.txt"
        target.write_text(normalised + "\n", encoding="utf-8")

        if not normalised:
            empty.append(i)

        index.append(
            {
                "page": i,
                "file": f"page_text/page_{i:02d}.txt",
                "chars": len(normalised),
                "sha256": hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
            }
        )

    (OUT / "INDEX.json").write_text(
        json.dumps(
            {
                "document_id": "TU_STUDENT_GUIDE_V3_1447",
                "source_file_sha256": EXPECTED_SHA256,
                "extractor": "pypdf",
                "extractor_note": "machine text-layer extraction, no model in the loop",
                "is_verbatim": True,
                "covers": "wording only — NOT table structure or reading order",
                "pages_total": len(reader.pages),
                "pages_with_no_text_layer": empty,
                "pages": index,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"extracted {len(index)} pages -> {OUT}")
    print(f"pages with no text layer: {empty or 'none'}")
    print(f"total chars: {sum(int(p['chars']) for p in index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
