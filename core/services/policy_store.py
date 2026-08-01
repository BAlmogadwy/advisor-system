"""Read-only access to the approved policy store under ``policies/``.

The store held 81 approved rule records for a week without a single line of runtime
code reading them. This module is the seam that changes that, and it is deliberately
narrow: it *retrieves and cites*. It never decides.

Three properties the callers depend on:

**Only approved records leave this module.** ``lookup()`` filters on
``verification.status == AUTHORITY_APPROVED`` before anything else. A record in any
earlier stage is invisible to the runtime, not merely deprioritised.

**Retrieval is deterministic.** Topic keys, a curated Arabic alias table and token
overlap — no embeddings, no ranking model. The same question returns the same
records on every machine, which is what makes ``policy-resolution recall`` a
measurable number rather than a sample of one. If evaluation ever shows this is
insufficient, that is the evidence for changing it; nothing here should change
before then.

**Provenance travels with the rule.** Every returned record carries its document,
page, edition and approval state, because the answer contract requires the model to
cite and :func:`validate_citations` refuses anything it cannot match back to a
record that was actually returned. A citation the model invents is rejected by
comparison, not by trust.

What this module deliberately does NOT do is judge whether a rule may be applied to
a student. Each record answers that itself through ``runtime_use``, which is surfaced
verbatim as ``decision_use``. 20 of the 81 records are
``PROHIBITED_FOR_DECISION`` — the inputs their conditions need do not exist in the
schema — and for those the correct answer explains the rule and stops.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings

from core.services.arabic_text import content_tokens

APPROVED_STATUS = "AUTHORITY_APPROVED"

#: Minimum normalised-IDF overlap for a lexical match to count (1.0 = one term
#: unique to a single record). Drops records sharing
#: only a filler word with the question; costs ~1pp of recall and buys ~2pp of
#: precision on the 284-question set. A curated topic alias bypasses it.
#:
#: It is NOT an abstention mechanism, and no retrieval threshold can be one here.
#: The questions with no answering policy (lateness counting as absence, repeating a
#: course, lab section numbering) are squarely ON topic — the obtained sources simply
#: do not state the rule. Their neighbouring records SHOULD be retrieved. Tightening
#: this until those questions returned nothing cost 95 of 252 genuine answers and
#: still missed most of them. Recognising that nothing returned actually answers the
#: question is the answer contract's duty, not the retriever's.
MIN_LEXICAL_WEIGHT = 0.45

#: Files under ``policies/`` that are not rule records.
_SKIP_FILES = frozenset({"sources.yaml", "evidence_map.yaml", "topic_aliases.yaml"})

#: Sub-trees that are not rule records: raw extraction, helper scripts, the source
#: documents themselves, and the calendar (events, not rules — a different shape
#: with its own capability).
_SKIP_DIRS = frozenset({"evidence", "tools", "sources", "calendar"})

#: A record with no ``runtime_use`` is a statement of fact, not a decision rule —
#: there is nothing to evaluate against a student, so explaining it is the whole
#: correct answer.
_DECISION_USE_DEFAULT = "EXPLANATORY_ONLY"

#: Every value the answer contract in SYSTEM_PROMPT_AGENT explains. Validated at
#: load so a new one cannot arrive undocumented.
DECISION_USE_VALUES = frozenset(
    {
        "PROHIBITED_FOR_DECISION",
        "PARTIALLY_EVALUABLE",
        "PERMITTED_WITH_USER_PROVIDED_INPUTS",
        _DECISION_USE_DEFAULT,
    }
)

#: Engineering annotations, not policy text. They name database tables, quote row
#: counts and counts of students by status, and reference internal tools and eval
#: question ids. Useful to an operator debugging why a rule cannot be applied;
#: never appropriate to put in front of a student.
OPERATOR_ONLY_FIELDS = (
    "runtime_use_reason",
    "runtime_use_note",
    "never_infer",
    "open_question",
    "notes",
)


def _seen_twice(records: list[dict[str, Any]], policy_id: str) -> bool:
    return sum(1 for r in records if r.get("policy_id") == policy_id) > 1


#: Fields a citation must carry. Kept as a constant because the answer contract in
#: the system prompt, the validator and the tests must not drift apart.
CITATION_FIELDS: tuple[str, ...] = (
    "policy_id",
    "document_id",
    "document_title",
    "edition",
    "page",
    "effective_from",
    "effective_to",
)


def policy_root() -> Path:
    """Where the store lives. Overridable so tests can build a synthetic store."""
    configured = getattr(settings, "POLICY_STORE_ROOT", None)
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "policies"


def _parse_date(value: Any) -> dt.date | None:
    """ISO dates only, and ``None`` for everything else — including Hijri years.

    ``source_edition`` and the source registry's ``effective_from`` both carry bare
    Hijri years such as ``"1447"``. Parsed leniently, ``1447`` becomes a Gregorian
    year in the distant past and every record silently reads as expired. Returning
    ``None`` makes an uncomparable date *unknown*, which callers treat as
    open-ended rather than lapsed.
    """
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _pages(record: dict[str, Any]) -> list[int]:
    """Normalise ``source.page`` / ``source.pages`` to one list."""
    source = record.get("source") or {}
    raw = source.get("pages")
    if raw is None:
        raw = source.get("page")
    if raw is None:
        return []
    if not isinstance(raw, list | tuple):
        raw = [raw]
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


#: Rule-shaped keys. Collected into one ``rule`` block so the model sees the
#: operative content as a unit instead of hunting for whichever of these a given
#: record happens to use.
_RULE_KEYS = (
    "rule_type",
    "condition",
    "conditions",
    "effect",
    "trigger",
    "unit",
    "min_value",
    "max_value",
    "result_if_false",
    "terms",
    "scope",
    "applies_to",
    "channel",
    "requires_approval",
    "listed_under",
    "required_inputs",
    "deadline_defined_in",
    "window_defined_in",
    "calendar_binding",
)


#: Keys carrying bookkeeping rather than policy text. Everything else in a record
#: is indexed, because 11 records have no ``source_text_ar`` at all and keep their
#: entire content in structured fields — the terminology definitions, the grade
#: table, the اعتذار/تأجيل/انسحاب comparison. Indexing title + source_text alone
#: reached 28% of the store's own words and made those 11 records unreachable by
#: anything but their four-word titles.
_INDEX_SKIP_KEYS = frozenset(
    {
        "verification",
        "policy_id",
        "source",
        "authority_level",
        "source_edition",
        "policy_effective_from",
        "policy_effective_to",
        "currentness_status",
        "extraction_confidence",
        "runtime_use",
        "cross_references",
        "superseded_by",
        "calendar_binding",
        "deadline_defined_in",
        "window_defined_in",
    }
)

#: Genuine vocabulary differences between how the guide writes and how a student
#: types, folded to a shared form. Kept deliberately short: each pair is a synonym
#: a student actually used in the 284-question set, not a thesaurus. Tokens are
#: already Arabic-normalised here (ة->ه, أ->ا), so entries are in folded form.
_SYNONYMS: dict[str, str] = {
    "ماده": "مقرر",
    "مواد": "مقرر",
    "مقررات": "مقرر",
    "مقرر": "مقرر",
    "ترم": "فصل",
    "فصول": "فصل",
    "فصل": "فصل",
    "ساعه": "وحده",
    "ساعات": "وحده",
    "وحدات": "وحده",
    "وحده": "وحده",
    "درجه": "تقدير",
    "درجات": "تقدير",
    "تقديرات": "تقدير",
    "تقدير": "تقدير",
    "gpa": "معدل",
    "معدلي": "معدل",
    "المعدل": "معدل",
    "سحب": "انسحاب",
    "انسحب": "انسحاب",
    "اعتذر": "اعتذار",
    "اجل": "تاجيل",
    "ااجل": "تاجيل",
    "اوجل": "تاجيل",
    "تاجيل": "تاجيل",
    "انفصل": "فصل_جامعه",
    "يفصلوني": "فصل_جامعه",
    "مفصول": "فصل_جامعه",
    "غياب": "غياب",
    "غبت": "غياب",
}

#: Prefixes a light stemmer may strip. Arabic attaches the article and several
#: conjunctions directly to the word, so «المقررات» and «مقررات» are the same term
#: to a reader and different strings to a set intersection.
_PREFIXES = ("وال", "بال", "كال", "فال", "ال", "و", "ب", "ل", "ف")

#: Suffixes that mark number/gender rather than a different concept.
_SUFFIXES = ("يه", "ات", "ين", "ون", "ها", "هم", "ه")


def _variants(token: str) -> set[str]:
    """A token plus its light morphological variants. Deterministic, no lexicon.

    Both the query side and the record side are expanded, so a match needs a shared
    variant — this widens recall without letting an unrelated pair collide on a
    two-letter root.
    """
    out = {token}
    stem = token
    for prefix in _PREFIXES:
        if stem.startswith(prefix) and len(stem) - len(prefix) >= 3:
            stem = stem[len(prefix) :]
            out.add(stem)
            break
    for suffix in _SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 3:
            out.add(stem[: -len(suffix)])
            break
    return {_SYNONYMS.get(v, v) for v in out}


def _alias_words(alias: str) -> list[set[str]]:
    """One variant set per content word of an alias."""
    return [_variants(token) for token in content_tokens(alias)]


def expand_tokens(text: str) -> set[str]:
    """Content tokens plus their variants, for both queries and records."""
    expanded: set[str] = set()
    for token in content_tokens(text):
        expanded |= _variants(token)
    return expanded


def _index_text(obj: Any, *, top: bool = True) -> list[str]:
    """Every piece of human-readable text in a record, for the token index."""
    chunks: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if top and key in _INDEX_SKIP_KEYS:
                continue
            chunks.append(str(key).replace("_", " "))
            chunks.extend(_index_text(value, top=False))
    elif isinstance(obj, list | tuple):
        for item in obj:
            chunks.extend(_index_text(item, top=False))
    elif isinstance(obj, str):
        chunks.append(obj)
    return chunks


class PolicyStore:
    """An immutable, loaded view of the policy store."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        sources: dict[str, dict[str, Any]],
        precedence: list[str],
        conflicts: list[dict[str, Any]],
        aliases: dict[str, list[str]],
    ) -> None:
        self.records = records
        self.sources = sources
        self.precedence = precedence
        self.conflicts = conflicts
        self.aliases = aliases

        duplicates = sorted(
            {r["policy_id"] for r in records}
            & {p for p in (x["policy_id"] for x in records) if _seen_twice(records, p)}
        )
        if duplicates:
            # Last-write-wins across rglob order meant an unapproved redraft in a
            # later-sorting file could supply the BODY for an id whose approved copy
            # passed the gate. Redrafting a record into a second file is the normal
            # editing motion for a YAML store, so this has to be loud.
            raise ValueError(
                "Duplicate policy_id in the policy store: "
                + ", ".join(duplicates)
                + ". Each rule must exist exactly once; delete or merge the copies."
            )
        self.by_id = {r["policy_id"]: r for r in records}
        self._approved_by_id = {r["policy_id"]: r for r in records if self.is_approved(r)}
        self.by_topic: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            self.by_topic.setdefault(record["topic"], []).append(record)

        # policy_id -> conflicts naming it, on either side.
        self.conflicts_by_policy: dict[str, list[dict[str, Any]]] = {}
        for conflict in conflicts:
            for side in ("lower_authority", "higher_authority"):
                pid = (conflict.get(side) or {}).get("policy_id")
                if pid:
                    self.conflicts_by_policy.setdefault(pid, []).append(conflict)

        # Pre-tokenise once. Retrieval runs per request; parsing does not.
        self._tokens: dict[str, set[str]] = {
            r["policy_id"]: expand_tokens(" ".join(_index_text(r))) for r in records
        }

        # Inverse document frequency. «مقرر» and «طالب» appear in most of the store
        # and say nothing about which rule is meant; «حرمان» appears in two and says
        # almost everything. Without this a question about parking fees matches a
        # graduation-documents record on one shared filler word, and because SOME
        # match always exists the abstention path can never fire.
        document_count = max(1, len(records))
        frequency: dict[str, int] = {}
        for tokens in self._tokens.values():
            for token in tokens:
                frequency[token] = frequency.get(token, 0) + 1
        # Normalised to [0, 1] by dividing through by log(N). Raw IDF scales with
        # corpus size — log(81) is 4.4, log(5) is 1.6 — so an absolute threshold
        # against raw weights silently means something different for a different
        # store, and would have to be re-tuned every time a record was added.
        scale = math.log(document_count) or 1.0
        self._idf: dict[str, float] = {
            token: math.log(document_count / count) / scale for token, count in frequency.items()
        }
        # Per ALIAS, a list of per-WORD variant sets. Merging every word's variants
        # into one set and testing subset fails on the first word whose particular
        # spelling the student did not use: «الاعتذار عن الفصل» contributes both
        # «الاعتذار» and «اعتذار», and a student writing «أعتذر» supplies only the
        # second, so the merged set is never a subset. Matching word by word asks
        # the right question — is each alias word present in SOME form?
        self._alias_tokens: dict[str, list[list[set[str]]]] = {
            topic: [words for words in (_alias_words(a) for a in als) if words]
            for topic, als in aliases.items()
        }

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, root: Path | str | None = None) -> PolicyStore:
        base = Path(root) if root else policy_root()

        registry = yaml.safe_load((base / "sources.yaml").read_text(encoding="utf-8")) or {}
        sources = {s["document_id"]: s for s in registry.get("sources") or []}
        precedence = list(registry.get("authority_precedence") or [])
        conflicts = list(registry.get("conflicts") or [])

        alias_path = base / "topic_aliases.yaml"
        aliases: dict[str, list[str]] = {}
        if alias_path.exists():
            doc = yaml.safe_load(alias_path.read_text(encoding="utf-8")) or {}
            for entry in doc.get("topics") or []:
                aliases[entry["topic"]] = list(entry.get("aliases_ar") or [])

        records: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*.yaml")):
            if path.name in _SKIP_FILES or _SKIP_DIRS & set(path.relative_to(base).parts):
                continue
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                records.extend(r for r in loaded if isinstance(r, dict) and r.get("policy_id"))

        unknown_use = sorted(
            {
                str(r.get("runtime_use"))
                for r in records
                if r.get("runtime_use") and str(r.get("runtime_use")) not in DECISION_USE_VALUES
            }
        )
        if unknown_use:
            # The answer contract explains four values. A fifth would reach the model
            # with no instruction attached, and an unexplained authority label reads
            # as permission.
            raise ValueError(
                "Unknown runtime_use value(s) in the policy store: "
                + ", ".join(unknown_use)
                + f". Known values: {', '.join(sorted(DECISION_USE_VALUES))}. "
                "Add the value to the answer contract before adding it to a record."
            )

        return cls(records, sources, precedence, conflicts, aliases)

    # ------------------------------------------------------------- provenance

    def tokens_for(self, policy_id: str) -> set[str]:
        """The indexed tokens for a record. Exposed so the recall harness can
        classify why a policy was or was not reachable without reaching inside."""
        return set(self._tokens.get(policy_id, set()))

    def precedence_rank(self, authority_level: str) -> int:
        """Lower is stronger. Unknown levels sort last rather than first."""
        try:
            return self.precedence.index(authority_level)
        except ValueError:
            return len(self.precedence)

    def is_approved(self, record: dict[str, Any]) -> bool:
        return str((record.get("verification") or {}).get("status") or "") == APPROVED_STATUS

    def effective_window(self, record: dict[str, Any]) -> tuple[dt.date | None, dt.date | None]:
        return (
            _parse_date(record.get("policy_effective_from")),
            _parse_date(record.get("policy_effective_to")),
        )

    def is_expired(self, record: dict[str, Any], as_of: dt.date) -> bool:
        _, until = self.effective_window(record)
        return until is not None and until < as_of

    def citation_for(self, record: dict[str, Any]) -> dict[str, Any]:
        """The citation a model is permitted to emit for this record."""
        source = record.get("source") or {}
        document_id = str(source.get("document_id") or "")
        registry = self.sources.get(document_id) or {}
        pages = _pages(record)
        since, until = self.effective_window(record)
        return {
            "policy_id": record["policy_id"],
            "document_id": document_id,
            "document_title": registry.get("title_ar") or registry.get("title_en") or document_id,
            "edition": str(record.get("source_edition") or registry.get("version") or "").strip()
            or None,
            "page": pages[0] if len(pages) == 1 else (pages or None),
            "effective_from": since.isoformat() if since else record.get("policy_effective_from"),
            "effective_to": until.isoformat() if until else record.get("policy_effective_to"),
        }

    def present(
        self,
        record: dict[str, Any],
        as_of: dt.date,
        *,
        include_operator_notes: bool = False,
    ) -> dict[str, Any]:
        """The full shape handed to the model for one policy.

        ``include_operator_notes`` defaults to FALSE so that forgetting to pass it
        withholds the internal annotations rather than publishing them.
        """
        rule = {k: record[k] for k in _RULE_KEYS if k in record}
        source = record.get("source") or {}
        document_id = str(source.get("document_id") or "")
        registry = self.sources.get(document_id) or {}
        verification = record.get("verification") or {}

        payload: dict[str, Any] = {
            "policy_id": record["policy_id"],
            "topic": record["topic"],
            "title_ar": record.get("title_ar"),
            "statement_ar": record.get("source_text_ar"),
            "rule": rule,
            "exceptions": record.get("exceptions") or [],
            "source": {
                "document_id": document_id,
                "document_title_ar": registry.get("title_ar"),
                "publisher_ar": registry.get("publisher_ar"),
                "edition": record.get("source_edition") or registry.get("version"),
                "pages": _pages(record),
            },
            "authority": {
                "level": record.get("authority_level"),
                "precedence_rank": self.precedence_rank(str(record.get("authority_level") or "")),
                "approval_status": verification.get("status"),
                "approved_by": verification.get("authority_approved_by"),
                "approved_at": verification.get("authority_approved_at"),
            },
            "effective": {
                "from": record.get("policy_effective_from"),
                "to": record.get("policy_effective_to"),
                "currentness_status": record.get("currentness_status"),
                "expired": self.is_expired(record, as_of),
            },
            # Verbatim from the record. This is the store's own statement about
            # whether the rule may be applied to a student, and the runtime is not
            # entitled to soften it.
            "decision_use": record.get("runtime_use") or _DECISION_USE_DEFAULT,
            "citation": self.citation_for(record),
        }

        if include_operator_notes:
            for key in OPERATOR_ONLY_FIELDS:
                if record.get(key):
                    payload[key] = record[key]
        elif record.get("open_question"):
            # A student is entitled to know the source is unclear on a point — that
            # is about the rule. They are not entitled to the engineering note
            # explaining which database column is empty, which is about us.
            payload["source_is_unclear_on"] = (
                "The written source does not settle this point. Confirm with عمادة "
                "القبول والتسجيل before relying on it."
            )

        related = self.conflicts_by_policy.get(record["policy_id"])
        if related:
            payload["conflicts"] = [self._conflict_view(c, record["policy_id"]) for c in related]
        return payload

    def _conflict_view(self, conflict: dict[str, Any], policy_id: str) -> dict[str, Any]:
        lower = conflict.get("lower_authority") or {}
        this_side = "lower_authority" if lower.get("policy_id") == policy_id else "higher_authority"
        return {
            "conflict_id": conflict.get("id"),
            "subject": conflict.get("subject"),
            "this_policy_is": this_side,
            "governs": this_side == "higher_authority",
            "resolution": conflict.get("resolution"),
            "higher_authority_says": (conflict.get("higher_authority") or {}).get("says"),
            "caveat": conflict.get("caveat"),
        }

    # -------------------------------------------------------------- retrieval

    def resolve_topics(self, query: str) -> list[tuple[str, int]]:
        """Topics whose curated aliases are present in the query. Deterministic.

        Matching is on TOKENS, not substrings. A student writing «أبغى أعتذر عن
        الفصل» must reach the alias «الاعتذار عن الفصل», and substring matching
        cannot see through the prefix — the two share a stem and no literal span.
        An alias fires when all of its content tokens appear in the query, so the
        long aliases stay specific while tolerating the words between them.
        """
        q_tokens = expand_tokens(query)
        if not q_tokens:
            return []
        hits: list[tuple[str, int]] = []
        for topic, alias_tokens in self._alias_tokens.items():
            if topic not in self.by_topic:
                continue
            score = 0
            for words in alias_tokens:
                if all(variants & q_tokens for variants in words):
                    # Longer aliases are more specific: «الانسحاب من مقرر» should
                    # outrank a bare «انسحاب» that also matches university withdrawal.
                    score += len(words)
            if score:
                hits.append((topic, score))
        return sorted(hits, key=lambda x: (-x[1], x[0]))

    def lookup(
        self,
        *,
        topic: str | None = None,
        query: str | None = None,
        policy_ids: Sequence[str] | None = None,
        limit: int = 6,
        as_of: dt.date | None = None,
        include_expired: bool = False,
        include_operator_notes: bool = False,
    ) -> dict[str, Any]:
        """Approved policies matching a topic, an explicit id list, or Arabic text."""
        as_of = as_of or dt.date.today()
        limit = max(1, min(int(limit or 6), 20))

        approved = [r for r in self.records if self.is_approved(r)]
        excluded_unapproved = len(self.records) - len(approved)

        candidates: list[dict[str, Any]]
        matched_topics: list[str] = []
        strategy: str

        if policy_ids:
            wanted = [str(p).strip() for p in policy_ids if str(p).strip()]
            approved_ids = set(self._approved_by_id)
            candidates = [self._approved_by_id[p] for p in wanted if p in approved_ids]
            strategy = "policy_ids"
            unknown = [p for p in wanted if p not in self.by_id]
            withheld = [p for p in wanted if p in self.by_id and p not in approved_ids]
        elif topic:
            key = str(topic).strip()
            candidates = [r for r in self.by_topic.get(key, []) if self.is_approved(r)]
            matched_topics = [key] if candidates else []
            strategy = "topic"
            unknown, withheld = [], []
        elif query:
            topic_hits = self.resolve_topics(query)
            matched_topics = [t for t, _ in topic_hits]
            # Only the STRONGEST topic bypasses the lexical floor. resolve_topics
            # returns every topic whose aliases fired, and admitting all of them at
            # rank tier 0 let records sharing nothing with the question outrank real
            # lexical matches — 136 of 1799 returned records were below-floor
            # topic-only admissions, displacing an expected policy in 18 of 252
            # eval questions. A weaker topic still helps, it just has to earn its place.
            best_topic_score = topic_hits[0][1] if topic_hits else 0
            primary_topics = {t for t, score in topic_hits if score == best_topic_score}
            q_tokens = expand_tokens(query)
            scored: list[tuple[tuple[int, float, int], dict[str, Any]]] = []
            for record in approved:
                shared = q_tokens & self._tokens.get(record["policy_id"], set())
                # sorted(): float addition is order-dependent and set iteration
                # order is not fixed across processes, which is enough to reorder
                # near-ties and make "deterministic retrieval" quietly false.
                weight = sum(self._idf.get(token, 0.0) for token in sorted(shared))
                topic_hit = 1 if record["topic"] in primary_topics else 0
                # A curated topic alias is an explicit routing decision and always
                # qualifies. Lexical matches must clear both bars: enough absolute
                # signal, and enough of the question actually explained.
                if not topic_hit and weight < MIN_LEXICAL_WEIGHT:
                    continue
                # Topic match first, then weighted overlap, then authority. Stable
                # and explainable: no score blending that hides which signal fired.
                rank = (
                    -topic_hit,
                    -weight,
                    self.precedence_rank(str(record.get("authority_level") or "")),
                )
                scored.append((rank, record))
            scored.sort(key=lambda x: (x[0], x[1]["policy_id"]))
            candidates = [r for _, r in scored]
            strategy = "query"
            unknown, withheld = [], []
        else:
            return {
                "ok": False,
                "error": "Provide one of: topic, query, or policy_ids.",
            }

        live = (
            candidates
            if include_expired
            else [r for r in candidates if not self.is_expired(r, as_of)]
        )
        expired_excluded = len(candidates) - len(live)
        selected = live[:limit]

        policies = [
            self.present(r, as_of, include_operator_notes=include_operator_notes) for r in selected
        ]
        return {
            "ok": True,
            "strategy": strategy,
            "matched_topics": matched_topics,
            "policies": policies,
            "policy_count": len(policies),
            "total_matched": len(live),
            "truncated": len(live) > len(selected),
            "unknown_policy_ids": unknown,
            "withheld_unapproved_policy_ids": withheld,
            "excluded_unapproved_count": excluded_unapproved,
            "excluded_expired_count": expired_excluded,
            "as_of": as_of.isoformat(),
            "available_topics": sorted(self.by_topic),
            "citable": [p["citation"] for p in policies],
        }

    # ------------------------------------------------------------- citations

    def validate_citations(
        self,
        claimed: Iterable[dict[str, Any]],
        allowed: Iterable[dict[str, Any]] | None = None,
        *,
        as_of: dt.date | None = None,
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        """Check citations against the records, and against what was retrieved.

        ``allowed`` is the citation list returned by the ``lookup`` calls made during
        THIS request. A citation for a real, approved, current policy is still
        rejected if that policy was never retrieved — otherwise a model can recite a
        policy id from training and have it pass as grounded.
        """
        as_of = as_of or dt.date.today()
        allowed_ids = {c.get("policy_id") for c in (allowed or [])}

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for citation in claimed or []:
            pid = str((citation or {}).get("policy_id") or "").strip()
            record = self.by_id.get(pid)
            if not record:
                rejected.append({"citation": citation, "reason": "UNKNOWN_POLICY"})
                continue
            if not self.is_approved(record):
                rejected.append({"citation": citation, "reason": "NOT_APPROVED"})
                continue
            if allowed is not None and pid not in allowed_ids:
                rejected.append({"citation": citation, "reason": "NOT_RETRIEVED_THIS_REQUEST"})
                continue
            if not allow_expired and self.is_expired(record, as_of):
                rejected.append({"citation": citation, "reason": "EXPIRED"})
                continue

            truth = self.citation_for(record)
            claimed_page = citation.get("page")
            if claimed_page is not None:
                pages = _pages(record)
                supplied = claimed_page if isinstance(claimed_page, list) else [claimed_page]
                try:
                    supplied_int = [int(p) for p in supplied]
                except (TypeError, ValueError):
                    supplied_int = []
                if not supplied_int or any(p not in pages for p in supplied_int):
                    rejected.append({"citation": citation, "reason": "PAGE_NOT_IN_RECORD"})
                    continue
            for field, reason in (
                ("document_id", "DOCUMENT_MISMATCH"),
                ("edition", "EDITION_MISMATCH"),
            ):
                claimed_value = citation.get(field)
                if claimed_value is not None and str(claimed_value).strip() != str(
                    truth.get(field) or ""
                ):
                    rejected.append({"citation": citation, "reason": reason})
                    break
            else:
                accepted.append(truth)

        return {
            "ok": not rejected,
            "accepted": accepted,
            "rejected": rejected,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
        }


_lock = threading.Lock()
_cache: PolicyStore | None = None


def get_policy_store(*, refresh: bool = False) -> PolicyStore:
    """Process-wide cached store. The YAML does not change between deploys."""
    global _cache
    with _lock:
        if _cache is None or refresh:
            _cache = PolicyStore.load()
        return _cache


def reset_policy_store_cache() -> None:
    """Drop the cache. For tests that point ``POLICY_STORE_ROOT`` elsewhere."""
    global _cache
    with _lock:
        _cache = None
