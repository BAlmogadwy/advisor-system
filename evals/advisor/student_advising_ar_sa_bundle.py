"""Audit and score the de-identified Saudi-Arabic advising bundle.

This adapter is intentionally narrower than the generic V2.1 planner contract.  The
bundle describes logical university data sources and answer-level behavior, while the
runtime exposes compound read-only capabilities.  Only records with an unambiguous,
currently representable V2.1 plan are emitted for planner scoring.  Unsupported live,
write, and adjudication cases remain visible in the coverage report instead of being
silently counted as model failures.

The source archive is read in place; prompts are never copied into this repository.
No provider calls are made by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

JSONL_NAME = "student_advising_eval_ar_sa.jsonl"
SCHEMA_NAME = "student_advising_eval_ar_sa.schema.json"
README_NAME = "README.md"
EXPECTED_ENTRIES = frozenset({JSONL_NAME, SCHEMA_NAME, README_NAME})
MAX_ARCHIVE_BYTES = 2_000_000
MAX_UNCOMPRESSED_BYTES = 5_000_000

V21_TOOL_SURFACE = frozenset(
    {
        "my_progress",
        "my_plan_by_term",
        "my_timetable",
        "my_clash_free_sections",
        "build_timetable_proposal",
        "lookup_course",
        "course_prerequisites",
        "why_course_locked",
        "course_choice_comparison",
        "feasible_course_replacements",
        "recommend_courses",
        "graduation_progress",
        "policy_lookup",
        "my_advisor",
    }
)

TRANSACTIONAL_ROOTS = frozenset({"root_031", "root_032"})
ADJUDICATION_ROOTS = frozenset({"root_010", "root_017", "root_029", "root_058"})

# These six roots have one stable, fully representable plan for all three prompts.
DIRECT_ROOT_PLANS: Mapping[str, tuple[tuple[str, Mapping[str, Any]], ...]] = {
    "root_007": (("policy_lookup", {}),),
    "root_009": (("my_plan_by_term", {}),),
    "root_027": (
        ("my_timetable", {}),
        ("my_plan_by_term", {}),
        ("policy_lookup", {}),
    ),
    "root_033": (
        ("my_timetable", {}),
        ("lookup_course", {"query": "برمجة ٢"}),
    ),
    "root_038": (("policy_lookup", {}),),
    "root_057": (("my_timetable", {}), ("policy_lookup", {})),
}

# Independent Arabic adjudication found three inherited root-level plans to be
# unnecessarily broad for the exact utterance. These overrides are answer-safe:
# they still require the capability that owns the requested fact/rule.
RECORD_PLAN_OVERRIDES: Mapping[str, tuple[tuple[str, Mapping[str, Any]], ...]] = {
    "sa_adv_000081": (("my_timetable", {}),),
    "sa_adv_000169": (("policy_lookup", {}),),
    "sa_adv_000171": (("policy_lookup", {}),),
}

_COURSE_MATH_204 = re.compile(r"(?<![A-Za-z0-9])MATH\s*204(?![A-Za-z0-9])", re.I)
_SAFE_ID = re.compile(r"sa_adv_\d{6}\Z")
_SAFE_ROOT = re.compile(r"root_\d{3}\Z")
_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class BundleValidationError(ValueError):
    """The archive or its root/provenance contract is invalid."""


@dataclass(frozen=True)
class BundleRecord:
    """The small, non-PII projection needed by the adapter."""

    case_id: str
    root_id: str
    split: str
    provenance_type: str
    question: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class BundleData:
    """Validated archive rows plus reproducibility metadata."""

    path: pathlib.Path
    sha256: str
    rows: tuple[BundleRecord, ...]


def _archive_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_safe_archive(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        raise BundleValidationError("bundle path is not a file")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BundleValidationError("bundle archive exceeds the size limit")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleValidationError("bundle is not a readable ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise BundleValidationError("bundle contains duplicate entry names")
        if set(names) != EXPECTED_ENTRIES:
            raise BundleValidationError("bundle entries do not match the closed contract")
        if any(
            info.is_dir()
            or info.flag_bits & 0x1
            or pathlib.PurePosixPath(info.filename).is_absolute()
            or len(pathlib.PurePosixPath(info.filename).parts) != 1
            or "\\" in info.filename
            for info in infos
        ):
            raise BundleValidationError("bundle contains an unsafe entry")
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
            raise BundleValidationError("bundle expands beyond the size limit")
        decoded: dict[str, str] = {}
        for info in infos:
            try:
                decoded[info.filename] = archive.read(info).decode("utf-8", errors="strict")
            except (OSError, RuntimeError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
                raise BundleValidationError(
                    "bundle entry is unreadable or not strict UTF-8"
                ) from exc
        return decoded


def _record(raw: Any, *, line_number: int) -> BundleRecord:
    if not isinstance(raw, Mapping):
        raise BundleValidationError(f"JSONL line {line_number} is not an object")
    case_id = str(raw.get("id") or "")
    split = str(raw.get("split") or "")
    language = str(raw.get("language") or "")
    question = str(raw.get("utterance_ar") or "").strip()
    provenance = raw.get("provenance")
    if not _SAFE_ID.fullmatch(case_id):
        raise BundleValidationError(f"JSONL line {line_number} has an invalid id")
    if split not in {"dev", "test"} or language != "ar-SA" or not question:
        raise BundleValidationError(f"JSONL line {line_number} has invalid core fields")
    if raw.get("pii_removed") is not True or not isinstance(provenance, Mapping):
        raise BundleValidationError(f"JSONL line {line_number} violates privacy/provenance fields")
    root_id = str(provenance.get("root_id") or "")
    provenance_type = str(provenance.get("type") or "")
    if not _SAFE_ROOT.fullmatch(root_id) or provenance_type not in {
        "observed_normalized",
        "synthetic_variant",
    }:
        raise BundleValidationError(f"JSONL line {line_number} has invalid provenance")
    return BundleRecord(
        case_id=case_id,
        root_id=root_id,
        split=split,
        provenance_type=provenance_type,
        question=question,
        raw=dict(raw),
    )


def _validate_roots(rows: Sequence[BundleRecord], *, strict_release: bool) -> None:
    ids = [row.case_id for row in rows]
    if len(ids) != len(set(ids)):
        raise BundleValidationError("bundle contains duplicate case ids")
    roots: dict[str, list[BundleRecord]] = defaultdict(list)
    for row in rows:
        roots[row.root_id].append(row)
    for root_id, members in roots.items():
        if len({row.split for row in members}) != 1:
            raise BundleValidationError(f"{root_id} crosses dataset splits")
        observed = [row for row in members if row.provenance_type == "observed_normalized"]
        synthetic = [row for row in members if row.provenance_type == "synthetic_variant"]
        if len(observed) != 1:
            raise BundleValidationError(f"{root_id} must have exactly one observed record")
        observed_id = observed[0].case_id
        for row in synthetic:
            variant_of = str((row.raw.get("provenance") or {}).get("variant_of") or "")
            if variant_of != observed_id:
                raise BundleValidationError(f"{row.case_id} has an invalid variant parent")
        if strict_release and (len(members) != 3 or len(synthetic) != 2):
            raise BundleValidationError(f"{root_id} does not contain one root plus two variants")
    if strict_release:
        if len(rows) != 180 or len(roots) != 60:
            raise BundleValidationError("release bundle must contain 180 records in 60 roots")
        splits = Counter(row.split for row in rows)
        provenance = Counter(row.provenance_type for row in rows)
        intents = Counter(str(row.raw.get("intent") or "") for row in rows)
        if splits != {"dev": 90, "test": 90}:
            raise BundleValidationError("release split must be 90 dev / 90 test")
        if provenance != {"observed_normalized": 60, "synthetic_variant": 120}:
            raise BundleValidationError("release provenance distribution is invalid")
        if len(intents) != 30 or set(intents.values()) != {6}:
            raise BundleValidationError("release must contain 30 intents with six records each")


def load_bundle(path: str | pathlib.Path, *, strict_release: bool = True) -> BundleData:
    """Read and validate the archive without extracting it."""

    bundle_path = pathlib.Path(path).resolve()
    entries = _read_safe_archive(bundle_path)
    rows: list[BundleRecord] = []
    for line_number, line in enumerate(entries[JSONL_NAME].splitlines(), start=1):
        if not line.strip():
            raise BundleValidationError(f"JSONL line {line_number} is blank")
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise BundleValidationError(f"JSONL line {line_number} is invalid JSON") from exc
        rows.append(_record(raw, line_number=line_number))
    _validate_roots(rows, strict_release=strict_release)
    return BundleData(path=bundle_path, sha256=_archive_sha256(bundle_path), rows=tuple(rows))


def record_category(record: BundleRecord) -> str:
    """Return the audited compatibility category for one prompt."""

    if record.root_id in TRANSACTIONAL_ROOTS:
        return "transactional_safety"
    if record.root_id in ADJUDICATION_ROOTS:
        return "adjudicate"
    if record.root_id in DIRECT_ROOT_PLANS:
        return "directly_scorable"
    if record.root_id == "root_004":
        # The third inherited paraphrase names only the Arabic course title.  Supplying
        # MATH204 from gold metadata would leak an entity the production planner never sees.
        return "directly_scorable" if _COURSE_MATH_204.search(record.question) else "capability_gap"
    return "capability_gap"


def expected_plan(record: BundleRecord) -> dict[str, Any] | None:
    """Return the closed planner expectation for a scorable/safety record."""

    category = record_category(record)
    if category == "transactional_safety":
        return {"decision": "direct", "evidence_requests": []}
    if category != "directly_scorable":
        return None
    calls: tuple[tuple[str, Mapping[str, Any]], ...]
    if record.root_id == "root_004":
        calls = (("why_course_locked", {"course_code": "MATH204"}),)
    elif record.case_id in RECORD_PLAN_OVERRIDES:
        calls = RECORD_PLAN_OVERRIDES[record.case_id]
    else:
        calls = DIRECT_ROOT_PLANS[record.root_id]
    return {
        "decision": "execute",
        "evidence_requests": [
            {"capability": capability, "arguments": dict(arguments)}
            for capability, arguments in calls
        ],
    }


def planner_cases(bundle: BundleData) -> list[dict[str, Any]]:
    """Project only the diagnostic subset; no gold metadata enters planner prompts."""

    return [
        {
            "id": row.case_id,
            "root_id": row.root_id,
            "split": row.split,
            "provenance_type": row.provenance_type,
            "language": "ar-SA",
            "question": row.question,
            "category": record_category(row),
        }
        for row in bundle.rows
        if expected_plan(row) is not None
    ]


def audit_report(bundle: BundleData) -> dict[str, Any]:
    """Return coverage without presenting correlated paraphrases as independent roots."""

    category_counts = Counter(record_category(row) for row in bundle.rows)
    roots: dict[str, set[str]] = defaultdict(set)
    for row in bundle.rows:
        roots[row.root_id].add(record_category(row))
    root_categories = Counter(
        next(iter(categories)) if len(categories) == 1 else "mixed_support"
        for categories in roots.values()
    )
    return {
        "bundle": {
            "path": str(bundle.path),
            "sha256": bundle.sha256,
            "records": len(bundle.rows),
            "roots": len(roots),
        },
        "compatibility": {
            "record_counts": dict(sorted(category_counts.items())),
            "root_counts": dict(sorted(root_categories.items())),
            "planner_diagnostic_records": len(planner_cases(bundle)),
            "primary_complete_roots": sum(
                categories <= {"directly_scorable", "transactional_safety"} and len(categories) == 1
                for categories in roots.values()
            ),
        },
        "limitations": [
            "The test split is visible and is no longer a blind holdout once inspected.",
            "Three paraphrases in one root are correlated; root-level rates are primary.",
            "No student, term, policy, section, or tool-output fixture is bundled.",
            "This adapter scores semantic evidence planning and write safety, not final-answer correctness.",
            "Live seats, offerings, registrar audit/equivalency, training, and writes remain outside V2.1.",
        ],
    }


def _text_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_DIGIT_TRANSLATION)
    return " ".join(text.casefold().split())


def _course_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text_key(value).upper())


def _normalise_plan(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return "", []
    decision = str(value.get("decision", value.get("mode", "")) or "").lower()
    calls = value.get("evidence_requests", value.get("tool_calls", []))
    if not isinstance(calls, list):
        return decision, []
    normalized: list[dict[str, Any]] = []
    for raw_call in calls:
        if not isinstance(raw_call, Mapping):
            normalized.append({"capability": "", "arguments": {}})
            continue
        function = raw_call.get("function")
        source = function if isinstance(function, Mapping) else raw_call
        capability = str(source.get("capability", source.get("name", "")) or "")
        arguments = source.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, UnicodeError, RecursionError):
                arguments = {}
        normalized.append(
            {
                "capability": capability,
                "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
            }
        )
    return decision, normalized


def _arguments_match(
    capability: str, actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    # policy_lookup.query is rebound to the exact question by the production server,
    # so its model-authored spelling is not part of this diagnostic.
    if capability == "policy_lookup":
        return not (set(actual) - {"query"})
    if set(actual) != set(expected):
        return False
    if capability == "why_course_locked":
        return _course_key(actual.get("course_code")) == _course_key(expected.get("course_code"))
    if capability == "lookup_course":
        return _text_key(actual.get("query")) == _text_key(expected.get("query"))
    return dict(actual) == dict(expected)


def score_plan(record: BundleRecord, actual_plan: Any) -> dict[str, Any]:
    """Score one record using tool-set minimality and canonical argument semantics."""

    expected = expected_plan(record)
    if expected is None:
        raise ValueError(f"{record.case_id} is not in the diagnostic subset")
    decision, actual_calls = _normalise_plan(actual_plan)
    expected_calls = list(expected["evidence_requests"])
    actual_names = [str(call["capability"]) for call in actual_calls]
    expected_names = [str(call["capability"]) for call in expected_calls]
    if record_category(record) == "transactional_safety":
        # V2.1 has no write capability. A read-only inspection/proposal is safe and
        # more useful than the current generic DIRECT greeting, even though it cannot
        # satisfy the bundle's confirmation/rollback answer contract. This dimension
        # scores only the planner boundary; final-answer suitability remains untested.
        mode_correct = decision in {"direct", "execute"}
        tools_correct = bool(set(actual_names) <= V21_TOOL_SURFACE)
        arguments_correct = tools_correct
    else:
        mode_correct = decision == expected["decision"]
        tools_correct = Counter(actual_names) == Counter(expected_names)
        arguments_correct = tools_correct and all(
            any(
                actual["capability"] == wanted["capability"]
                and _arguments_match(
                    str(wanted["capability"]),
                    actual["arguments"],
                    wanted["arguments"],
                )
                for actual in actual_calls
            )
            for wanted in expected_calls
        )
    dimensions = {
        "mode_correct": mode_correct,
        "tools_correct": tools_correct,
        "arguments_correct": arguments_correct,
    }
    return {
        "case_id": record.case_id,
        "root_id": record.root_id,
        "split": record.split,
        "provenance_type": record.provenance_type,
        "category": record_category(record),
        "safety_only": record_category(record) == "transactional_safety",
        "expected": expected,
        "actual": {"decision": decision, "evidence_requests": actual_calls},
        "dimensions": dimensions,
        "overall": all(dimensions.values()),
    }


def _planner_only_alternative_pass(record: BundleRecord, actual_plan: Any) -> bool:
    """Accept evidence the current renderer cannot yet expose as an answer.

    ``my_progress`` carries the student's remaining elective slots, so it is a
    semantically valid plan for sa_adv_000027. The deterministic V2.1 progress
    renderer does not currently present that field; it therefore belongs only in an
    explicitly labelled planner upper bound, never the conservative answer-safe rate.
    """

    if record.case_id != "sa_adv_000027":
        return False
    decision, calls = _normalise_plan(actual_plan)
    return bool(
        decision == "execute"
        and len(calls) == 1
        and calls[0]["capability"] == "my_progress"
        and calls[0]["arguments"] == {}
    )


def _result_rows(results: Any) -> dict[str, Any]:
    if isinstance(results, Mapping) and "rows" in results:
        results = results["rows"]
    if isinstance(results, Mapping):
        return {str(key): value for key, value in results.items()}
    if not isinstance(results, list):
        raise ValueError("candidate results must be an object or rows list")
    output: dict[str, Any] = {}
    for row in results:
        if not isinstance(row, Mapping):
            raise ValueError("candidate row must be an object")
        case_id = str(row.get("case_id", row.get("id", "")) or "")
        if not case_id or case_id in output:
            raise ValueError("candidate rows need unique case ids")
        output[case_id] = row.get("plan", row)
    return output


def score_results(bundle: BundleData, results: Any) -> dict[str, Any]:
    """Score a complete diagnostic run, reporting root and provenance strata."""

    by_id = {row.case_id: row for row in bundle.rows if expected_plan(row) is not None}
    candidate = _result_rows(results)
    missing = sorted(set(by_id) - set(candidate))
    unknown = sorted(set(candidate) - set(by_id))
    scored = [
        score_plan(by_id[case_id], candidate[case_id]) for case_id in by_id if case_id in candidate
    ]
    root_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        root_rows[str(row["root_id"])].append(row)
    root_coverage = Counter(case.root_id for case in by_id.values())
    complete_roots = [
        root_id
        for root_id, count in root_coverage.items()
        if count == 3 and len(root_rows.get(root_id, [])) == 3
    ]
    passed_roots = sum(all(item["overall"] for item in root_rows[root]) for root in complete_roots)

    def rate(passed: int, total: int) -> float:
        return round(passed / total, 6) if total else 0.0

    provenance_report: dict[str, Any] = {}
    for provenance in ("observed_normalized", "synthetic_variant"):
        subset = [row for row in scored if row["provenance_type"] == provenance]
        passed = sum(bool(row["overall"]) for row in subset)
        provenance_report[provenance] = {
            "passed": passed,
            "total": len(subset),
            "rate": rate(passed, len(subset)),
        }
    record_passed = sum(bool(row["overall"]) for row in scored)
    supported_rows = [row for row in scored if row["category"] == "directly_scorable"]
    transactional_rows = [row for row in scored if row["category"] == "transactional_safety"]
    supported_passed = sum(bool(row["overall"]) for row in supported_rows)
    transaction_passed = sum(bool(row["overall"]) for row in transactional_rows)
    planner_upper_passed = sum(
        bool(row["overall"])
        or _planner_only_alternative_pass(
            by_id[str(row["case_id"])], candidate[str(row["case_id"])]
        )
        for row in supported_rows
    )
    supported_full_roots = [
        root_id
        for root_id, count in root_coverage.items()
        if count == 3
        and all(
            by_id[case_id].root_id != root_id
            or record_category(by_id[case_id]) == "directly_scorable"
            for case_id in by_id
        )
        and len(root_rows.get(root_id, [])) == 3
    ]
    supported_roots_passed = sum(
        all(item["overall"] for item in root_rows[root]) for root in supported_full_roots
    )
    return {
        "coverage": {
            "expected_records": len(by_id),
            "scored_records": len(scored),
            "complete": not missing and not unknown,
            "missing": missing,
            "unknown": unknown,
        },
        "records": {
            "passed": record_passed,
            "total": len(scored),
            "rate": rate(record_passed, len(scored)),
        },
        "supported_answer_safe": {
            "passed": supported_passed,
            "total": len(supported_rows),
            "rate": rate(supported_passed, len(supported_rows)),
        },
        "supported_planner_only_upper_bound": {
            "passed": planner_upper_passed,
            "total": len(supported_rows),
            "rate": rate(planner_upper_passed, len(supported_rows)),
        },
        "transactional_read_only_safety": {
            "passed": transaction_passed,
            "total": len(transactional_rows),
            "rate": rate(transaction_passed, len(transactional_rows)),
            "answer_handoff_scored": False,
        },
        "supported_complete_roots": {
            "passed": supported_roots_passed,
            "total": len(supported_full_roots),
            "rate": rate(supported_roots_passed, len(supported_full_roots)),
        },
        "primary_complete_roots": {
            "passed": passed_roots,
            "total": len(complete_roots),
            "rate": rate(passed_roots, len(complete_roots)),
            "excluded_partial_roots": sorted(set(root_rows) - set(complete_roots)),
        },
        "by_provenance": provenance_report,
        "rows": scored,
    }


def _load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    parser.add_argument("--candidate", type=pathlib.Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    bundle = load_bundle(args.bundle)
    report: dict[str, Any] = {"audit": audit_report(bundle)}
    passed = True
    if args.candidate is not None:
        report["candidate"] = score_results(bundle, _load_json(args.candidate))
        passed = bool(report["candidate"]["coverage"]["complete"])
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
