"""The policy store and the citation contract.

Covers the nine behaviours the runtime integration is required to guarantee. Every
test here was checked by breaking the code it covers and confirming it goes red —
a test that passes against a mutated implementation is measuring nothing.

Most tests build a SYNTHETIC store on disk rather than using ``policies/``. The real
store has all 81 records approved and none expired, so exclusion of unapproved and
expired records is unobservable against it: the assertions would pass on an
implementation that never filtered at all.
"""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from core.services.policy_store import (
    DECISION_USE_VALUES,
    MIN_LEXICAL_WEIGHT,
    OPERATOR_ONLY_FIELDS,
    PolicyStore,
    expand_tokens,
    get_policy_store,
)
from core.services.virtual_advisor_capabilities import (
    ROLE_STUDENT,
    build_default_registry,
)

pytestmark = pytest.mark.django_db


# ── synthetic store ──────────────────────────────────────────────


def _record(policy_id, topic, *, status="AUTHORITY_APPROVED", page=10, **extra):
    record = {
        "policy_id": policy_id,
        "topic": topic,
        "title_ar": extra.pop("title_ar", "عنوان"),
        "authority_level": extra.pop("authority_level", "OFFICIAL_STUDENT_GUIDE"),
        "source": {"document_id": "DOC_GUIDE", "page": page},
        "rule_type": "limit",
        "source_edition": "3",
        "policy_effective_from": None,
        "policy_effective_to": None,
        "currentness_status": "UNVERIFIED",
        "verification": {"status": status},
        "extraction_confidence": "high",
    }
    record.update(extra)
    return record


@pytest.fixture
def synthetic(tmp_path):
    """A store with an unapproved record, an expired record, and a conflict."""
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "authority_precedence": [
                    "OFFICIAL_ACADEMIC_CALENDAR",
                    "OFFICIAL_STUDENT_GUIDE",
                ],
                "sources": [
                    {
                        "document_id": "DOC_GUIDE",
                        "title_ar": "الدليل الإرشادي",
                        "authority_level": "OFFICIAL_STUDENT_GUIDE",
                        "version": "3",
                    },
                    {
                        "document_id": "DOC_CAL",
                        "title_ar": "التقويم الأكاديمي",
                        "authority_level": "OFFICIAL_ACADEMIC_CALENDAR",
                    },
                ],
                "conflicts": [
                    {
                        "id": "CONFLICT.DEADLINE",
                        "subject": "when changes close",
                        "lower_authority": {
                            "document_id": "DOC_GUIDE",
                            "policy_id": "TU.REG.OLD_DEADLINE",
                            "says": "قبل بدء الدراسة بأسبوع",
                        },
                        "higher_authority": {
                            "document_id": "DOC_CAL",
                            "policy_id": "TU.CAL.NEW_DEADLINE",
                            "says": "قبل بدء الدراسة بيوم واحد",
                        },
                        "resolution": "التقويم الأكاديمي هو الحاكم.",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "topic_aliases.yaml").write_text(
        yaml.safe_dump(
            {"topics": [{"topic": "course_withdrawal", "aliases_ar": ["الانسحاب من مقرر"]}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(
            [
                _record(
                    "TU.WITHDRAWAL.MAXIMUM",
                    "course_withdrawal",
                    page=24,
                    title_ar="الحد الأقصى لعدد الانسحابات",
                    source_text_ar="لا يزيد عدد مرات الانسحاب من مقرر عن أربع مرات.",
                    max_value=4,
                ),
                _record(
                    "TU.WITHDRAWAL.DRAFT_RULE",
                    "course_withdrawal",
                    status="EXTRACTED",
                    page=24,
                    title_ar="قاعدة انسحاب غير معتمدة",
                    source_text_ar="مسودة عن الانسحاب من مقرر لم تُعتمد بعد.",
                ),
                _record(
                    "TU.WITHDRAWAL.LAPSED",
                    "course_withdrawal",
                    page=99,
                    title_ar="قاعدة انسحاب منتهية",
                    source_text_ar="قاعدة قديمة عن الانسحاب من مقرر.",
                    policy_effective_from="2020-01-01",
                    policy_effective_to="2021-12-31",
                ),
                _record(
                    "TU.REG.OLD_DEADLINE",
                    "registration_process",
                    page=20,
                    source_text_ar="تنتهي إجراءات التسجيل قبل بدء الدراسة بأسبوع.",
                ),
                _record(
                    "TU.CAL.NEW_DEADLINE",
                    "registration_process",
                    page=1,
                    authority_level="OFFICIAL_ACADEMIC_CALENDAR",
                    source_text_ar="الحذف والإضافة قبل بدء الدراسة بيوم.",
                ),
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return PolicyStore.load(tmp_path)


TODAY = dt.date(2026, 8, 1)


# ── 1. successful approved-policy lookup ─────────────────────────


def test_lookup_returns_approved_policy_with_full_provenance(synthetic):
    result = synthetic.lookup(query="كم مرة أقدر أنسحب من مقرر؟", as_of=TODAY)

    assert result["ok"]
    found = {p["policy_id"] for p in result["policies"]}
    assert "TU.WITHDRAWAL.MAXIMUM" in found

    policy = next(p for p in result["policies"] if p["policy_id"] == "TU.WITHDRAWAL.MAXIMUM")
    # Every field the answer contract requires, present and correct.
    assert policy["topic"] == "course_withdrawal"
    assert policy["title_ar"] == "الحد الأقصى لعدد الانسحابات"
    assert policy["rule"]["max_value"] == 4
    assert policy["source"]["document_id"] == "DOC_GUIDE"
    assert policy["source"]["pages"] == [24]
    assert policy["source"]["edition"] == "3"
    assert policy["authority"]["approval_status"] == "AUTHORITY_APPROVED"
    assert policy["authority"]["level"] == "OFFICIAL_STUDENT_GUIDE"
    assert policy["effective"]["expired"] is False
    assert policy["citation"]["page"] == 24
    assert policy["citation"]["document_title"] == "الدليل الإرشادي"


def test_topic_lookup_returns_only_that_topic(synthetic):
    result = synthetic.lookup(topic="registration_process", as_of=TODAY)
    assert {p["topic"] for p in result["policies"]} == {"registration_process"}
    assert result["strategy"] == "topic"


# ── 2. unknown policy topic ──────────────────────────────────────


def test_unknown_topic_returns_empty_not_error(synthetic):
    result = synthetic.lookup(topic="quidditch", as_of=TODAY)
    assert result["ok"] is True
    assert result["policies"] == []
    assert result["citable"] == []
    # The model needs the vocabulary to retry, not just a blank.
    assert "course_withdrawal" in result["available_topics"]


def test_unknown_policy_ids_are_named_not_silently_dropped(synthetic):
    result = synthetic.lookup(policy_ids=["TU.NOT.REAL", "TU.WITHDRAWAL.MAXIMUM"], as_of=TODAY)
    assert result["unknown_policy_ids"] == ["TU.NOT.REAL"]
    assert [p["policy_id"] for p in result["policies"]] == ["TU.WITHDRAWAL.MAXIMUM"]


def test_lookup_without_any_selector_is_an_error(synthetic):
    assert synthetic.lookup(as_of=TODAY)["ok"] is False


# ── 3. unapproved policy exclusion ───────────────────────────────


def test_unapproved_policy_never_returned_by_query(synthetic):
    result = synthetic.lookup(query="الانسحاب من مقرر", limit=20, as_of=TODAY)
    assert "TU.WITHDRAWAL.DRAFT_RULE" not in {p["policy_id"] for p in result["policies"]}
    assert result["excluded_unapproved_count"] == 1


def test_unapproved_policy_withheld_even_when_asked_for_by_id(synthetic):
    """The strongest form: naming the id directly must not bypass the filter."""
    result = synthetic.lookup(policy_ids=["TU.WITHDRAWAL.DRAFT_RULE"], as_of=TODAY)
    assert result["policies"] == []
    assert result["withheld_unapproved_policy_ids"] == ["TU.WITHDRAWAL.DRAFT_RULE"]
    # Withheld is not the same as unknown; the record exists.
    assert result["unknown_policy_ids"] == []


# ── 4. expired policy handling ───────────────────────────────────


def test_expired_policy_excluded_by_default(synthetic):
    result = synthetic.lookup(query="الانسحاب من مقرر", limit=20, as_of=TODAY)
    assert "TU.WITHDRAWAL.LAPSED" not in {p["policy_id"] for p in result["policies"]}
    assert result["excluded_expired_count"] == 1


def test_expired_policy_available_when_the_question_concerns_that_period(synthetic):
    result = synthetic.lookup(query="الانسحاب من مقرر", limit=20, as_of=TODAY, include_expired=True)
    lapsed = next(p for p in result["policies"] if p["policy_id"] == "TU.WITHDRAWAL.LAPSED")
    assert lapsed["effective"]["expired"] is True
    assert lapsed["effective"]["to"] == "2021-12-31"


def test_policy_current_at_an_earlier_date_is_not_expired_then(synthetic):
    result = synthetic.lookup(query="الانسحاب من مقرر", limit=20, as_of=dt.date(2021, 6, 1))
    assert "TU.WITHDRAWAL.LAPSED" in {p["policy_id"] for p in result["policies"]}


def test_hijri_year_string_is_not_read_as_an_expiry_date(synthetic):
    """A bare Hijri year must read as unknown, never as a lapsed Gregorian date.

    Parsed leniently, "1447" becomes a year in the distant past and every record in
    the store silently disappears as expired.
    """
    record = dict(synthetic.by_id["TU.WITHDRAWAL.MAXIMUM"], policy_effective_to="1447")
    assert synthetic.is_expired(record, TODAY) is False


# ── 5. conflicting policies ──────────────────────────────────────


def test_conflicting_policies_are_reported_with_an_explicit_resolution(synthetic):
    result = synthetic.lookup(topic="registration_process", as_of=TODAY)
    by_id = {p["policy_id"]: p for p in result["policies"]}

    # Neither side is silently dropped.
    assert {"TU.REG.OLD_DEADLINE", "TU.CAL.NEW_DEADLINE"} <= set(by_id)

    lower = by_id["TU.REG.OLD_DEADLINE"]["conflicts"][0]
    higher = by_id["TU.CAL.NEW_DEADLINE"]["conflicts"][0]
    assert lower["this_policy_is"] == "lower_authority"
    assert lower["governs"] is False
    assert higher["governs"] is True
    assert "التقويم" in lower["resolution"]
    assert lower["higher_authority_says"]


def test_authority_precedence_ranks_the_calendar_above_the_guide(synthetic):
    assert synthetic.precedence_rank("OFFICIAL_ACADEMIC_CALENDAR") < synthetic.precedence_rank(
        "OFFICIAL_STUDENT_GUIDE"
    )
    # An unrecognised level sorts last, never first.
    assert synthetic.precedence_rank("SOMEONES_BLOG") > synthetic.precedence_rank(
        "OFFICIAL_STUDENT_GUIDE"
    )


# ── 6. citation field preservation ───────────────────────────────


def test_citation_carries_every_contract_field(synthetic):
    citation = synthetic.citation_for(synthetic.by_id["TU.WITHDRAWAL.MAXIMUM"])
    assert citation == {
        "policy_id": "TU.WITHDRAWAL.MAXIMUM",
        "document_id": "DOC_GUIDE",
        "document_title": "الدليل الإرشادي",
        "edition": "3",
        "page": 24,
        "effective_from": None,
        "effective_to": None,
    }


def test_citable_list_matches_the_returned_policies_exactly(synthetic):
    result = synthetic.lookup(query="الانسحاب من مقرر", limit=20, as_of=TODAY)
    assert [c["policy_id"] for c in result["citable"]] == [
        p["policy_id"] for p in result["policies"]
    ]


def test_citable_tracks_the_truncated_selection_not_every_match(synthetic):
    """`citable` is permission to cite. It must not exceed what was actually shown.

    With limit >= the match count the two are equal whatever the code does, so the
    test above cannot distinguish "derived from the selected slice" from "derived
    from every match". Truncating is what makes the difference observable: deriving
    citable from all matches would hand the model permission to cite policies whose
    text it was never shown, and both citation checks would accept them.
    """
    # registration_process holds two approved, current records; the withdrawal topic
    # has only one once the unapproved and expired ones are filtered out, and a
    # single match cannot demonstrate truncation.
    result = synthetic.lookup(topic="registration_process", limit=1, as_of=TODAY)
    assert len(result["policies"]) == 1
    assert result["total_matched"] == 2
    assert result["truncated"] is True
    assert [c["policy_id"] for c in result["citable"]] == [
        p["policy_id"] for p in result["policies"]
    ]


# ── 7. fabricated citation rejection ─────────────────────────────


def test_citation_for_unretrieved_policy_is_rejected(synthetic):
    """A real, approved, current policy is still not citable if it was not fetched."""
    retrieved = synthetic.lookup(policy_ids=["TU.WITHDRAWAL.MAXIMUM"], as_of=TODAY)["citable"]
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.REG.OLD_DEADLINE"}], retrieved, as_of=TODAY
    )
    assert verdict["ok"] is False
    assert verdict["rejected"][0]["reason"] == "NOT_RETRIEVED_THIS_REQUEST"


def test_invented_policy_id_is_rejected(synthetic):
    verdict = synthetic.validate_citations([{"policy_id": "TU.MADE.UP"}], [], as_of=TODAY)
    assert verdict["rejected"][0]["reason"] == "UNKNOWN_POLICY"


def test_unapproved_policy_cannot_be_cited(synthetic):
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.WITHDRAWAL.DRAFT_RULE"}], None, as_of=TODAY
    )
    assert verdict["rejected"][0]["reason"] == "NOT_APPROVED"


def test_expired_policy_cannot_be_cited_by_default(synthetic):
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.WITHDRAWAL.LAPSED"}], None, as_of=TODAY
    )
    assert verdict["rejected"][0]["reason"] == "EXPIRED"


def test_wrong_page_for_a_real_policy_is_rejected(synthetic):
    retrieved = synthetic.lookup(policy_ids=["TU.WITHDRAWAL.MAXIMUM"], as_of=TODAY)["citable"]
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.WITHDRAWAL.MAXIMUM", "page": 23}], retrieved, as_of=TODAY
    )
    assert verdict["rejected"][0]["reason"] == "PAGE_NOT_IN_RECORD"


def test_wrong_edition_is_rejected(synthetic):
    retrieved = synthetic.lookup(policy_ids=["TU.WITHDRAWAL.MAXIMUM"], as_of=TODAY)["citable"]
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.WITHDRAWAL.MAXIMUM", "edition": "2"}], retrieved, as_of=TODAY
    )
    assert verdict["rejected"][0]["reason"] == "EDITION_MISMATCH"


def test_correct_citation_is_accepted_and_normalised(synthetic):
    retrieved = synthetic.lookup(policy_ids=["TU.WITHDRAWAL.MAXIMUM"], as_of=TODAY)["citable"]
    verdict = synthetic.validate_citations(
        [{"policy_id": "TU.WITHDRAWAL.MAXIMUM", "page": 24}], retrieved, as_of=TODAY
    )
    assert verdict["ok"] is True
    # The accepted citation is the store's version, not the model's.
    assert verdict["accepted"][0]["document_title"] == "الدليل الإرشادي"


# ── 8. policy-plus-student-data answers ──────────────────────────


def test_policy_lookup_is_student_reachable_alongside_the_data_tools():
    registry = build_default_registry()
    names = {c.name for c in registry.capabilities_for_scope({"role": ROLE_STUDENT})}
    assert "policy_lookup" in names
    # The point of the phase: rules AND the student's own data in one answer.
    assert {"my_progress", "my_timetable"} <= names


def test_policy_lookup_executes_through_the_registry_for_a_student():
    registry = build_default_registry()
    result = registry.execute(
        "policy_lookup", {"query": "كم مرة أقدر أنسحب من مقرر؟"}, scope={"role": ROLE_STUDENT}
    )
    assert result["ok"] is True
    assert result["tool"] == "policy_lookup"
    assert result["policies"], "the real store should answer a withdrawal question"
    assert all(
        p["authority"]["approval_status"] == "AUTHORITY_APPROVED" for p in result["policies"]
    )
    assert result["citable"]


# ── 9. unsupported-policy abstention ─────────────────────────────


def test_no_matching_policy_instructs_abstention_not_invention(monkeypatch, synthetic):
    """The empty-result branch must tell the model to abstain, not to improvise.

    Written against a SYNTHETIC store on purpose. The first version of this test ran
    the real store and wrapped its assertions in `if not result["policies"]:` — the
    real store answers almost any Arabic sentence with something, so the body never
    executed and the test passed without checking anything at all.
    """
    monkeypatch.setattr("core.services.policy_store.get_policy_store", lambda **kw: synthetic)
    result = build_default_registry().execute(
        "policy_lookup", {"query": "zzz nothing matches this"}, scope={"role": ROLE_STUDENT}
    )
    assert result["ok"] is True
    assert result["policies"] == [], "fixture must produce the empty branch"
    assert "no written rule" in result["note"]
    assert "Deanship" in result["note"] or "عمادة القبول والتسجيل" in result["note"]
    assert result["available_topics"], "the model needs vocabulary to retry with"


def test_non_empty_result_gets_the_citation_instruction_not_the_abstention_one(
    monkeypatch, synthetic
):
    monkeypatch.setattr("core.services.policy_store.get_policy_store", lambda **kw: synthetic)
    result = build_default_registry().execute(
        "policy_lookup", {"query": "الانسحاب من مقرر"}, scope={"role": ROLE_STUDENT}
    )
    assert result["policies"]
    assert "no written rule" not in result["note"]
    # The instruction now depends on whether anything RETRIEVED actually governs the
    # question, so the synthetic store's withdrawal query gets the direct-evidence
    # form. What must never appear here is the abstention text.
    assert "direct_policy_evidence" in result["note"] or "Cite ONLY" in result["note"]


def test_prohibited_for_decision_is_surfaced_verbatim():
    """20 records cannot be evaluated against a student. The runtime must say so."""
    store = get_policy_store()
    result = store.lookup(policy_ids=["TU.DISMISSAL.THREE_WARNINGS"])
    policy = result["policies"][0]
    assert policy["decision_use"] == "PROHIBITED_FOR_DECISION"
    # The prohibition itself reaches everyone; the engineering reason behind it does
    # not — see the operator-notes tests below.
    assert "runtime_use_reason" not in policy


def test_operator_notes_are_withheld_by_default():
    """Fail closed: forgetting the flag must withhold, never publish.

    These fields name database tables, quote row counts and quote counts of students
    by status. TU.DISMISSAL.THREE_WARNINGS' own reason mentions Student.status values
    and how many students hold them; TU.GPA.FORMULA's names a table and its row count.
    A student asking about their GPA must not be handed the schema.
    """
    store = get_policy_store()
    policy = store.lookup(policy_ids=["TU.DISMISSAL.THREE_WARNINGS"])["policies"][0]
    assert not set(policy) & set(OPERATOR_ONLY_FIELDS)


def test_operator_notes_are_available_to_staff():
    store = get_policy_store()
    policy = store.lookup(policy_ids=["TU.DISMISSAL.THREE_WARNINGS"], include_operator_notes=True)[
        "policies"
    ][0]
    assert policy["runtime_use_reason"], "an operator debugging this needs the reason"


def test_students_reach_no_operator_notes_through_the_capability():
    """The end-to-end path, because the flag is only useful if the executor sets it."""
    registry = build_default_registry()
    result = registry.execute(
        "policy_lookup", {"query": "كيف يحسب المعدل التراكمي؟"}, scope={"role": ROLE_STUDENT}
    )
    leaked = {k for p in result["policies"] for k in p if k in OPERATOR_ONLY_FIELDS}
    assert leaked == set(), f"internal annotations reached a student: {sorted(leaked)}"


def test_an_unclear_source_is_still_disclosed_to_the_student_in_their_own_terms():
    """Withholding the engineering note must not withhold the caveat itself."""
    store = get_policy_store()
    with_open_q = [r["policy_id"] for r in store.records if r.get("open_question")]
    assert with_open_q, "fixture assumption: some record carries an open_question"
    policy = store.lookup(policy_ids=[with_open_q[0]])["policies"][0]
    assert "source_is_unclear_on" in policy
    assert "open_question" not in policy


def test_records_without_runtime_use_are_marked_explanatory_not_decidable():
    store = get_policy_store()
    policy = store.lookup(policy_ids=["TU.GRADE.SCALE"])["policies"][0]
    assert policy["decision_use"] == "EXPLANATORY_ONLY"


# ── the real store's own invariants ──────────────────────────────


def test_real_store_loads_and_is_entirely_approved():
    store = get_policy_store()
    assert len(store.records) == 81
    assert all(store.is_approved(r) for r in store.records)
    assert len(store.by_topic) == 27


def test_structured_records_are_indexed_not_just_their_titles():
    """11 records keep their whole content in structured fields.

    Indexing title + source_text_ar alone reached 28% of the store's words and made
    the terminology, grade-scale and leave-comparison records unreachable.
    """
    store = get_policy_store()
    tokens = store.tokens_for("TU.DEF.PROGRAMME_AND_COURSES")
    assert not store.by_id["TU.DEF.PROGRAMME_AND_COURSES"].get("source_text_ar")
    # A word that appears only inside terms[].definition_ar.
    assert "اختياري" in tokens or "اختياريه" in tokens
    assert len(tokens) > 50


def test_query_matches_a_definition_held_in_structured_fields():
    store = get_policy_store()
    found = {
        p["policy_id"]
        for p in store.lookup(query="وش الفرق بين المادة الإجبارية والمادة الاختيارية؟")["policies"]
    }
    assert "TU.DEF.PROGRAMME_AND_COURSES" in found


def test_synonym_folding_bridges_student_and_guide_vocabulary():
    # The guide writes مقرر; students write مادة. Same concept, different string.
    assert expand_tokens("المادة") & expand_tokens("المقررات")
    # And it does not collapse unrelated words.
    assert not (expand_tokens("الغياب") & expand_tokens("التخرج"))


def test_alias_matches_a_verb_form_the_student_actually_types():
    """The alias reads «الاعتذار عن الفصل»; the student writes «أعتذر عن الفصل».

    Matching the alias as one merged token set fails here: the alias contributes
    both «الاعتذار» and «اعتذار» and the student supplies only the second, so the
    set is never a subset. Each alias word must be matched in its own right.
    """
    store = get_policy_store()
    topics = {t for t, _ in store.resolve_topics("أبغى أعتذر عن الفصل، وش الإجراء؟")}
    assert "semester_excuse" in topics
    found = {p["policy_id"] for p in store.lookup(query="أبغى أعتذر عن الفصل؟")["policies"]}
    assert "TU.EXCUSE.PROCEDURE" in found


def test_summer_credit_question_reaches_the_summer_rule():
    store = get_policy_store()
    found = [p["policy_id"] for p in store.lookup(query="كم ساعة أقدر أسجل في الصيفي؟")["policies"]]
    assert "TU.LOAD.SUMMER_MAX" in found
    # And it ranks above the general load rules, not merely somewhere in the list.
    assert found[0] == "TU.LOAD.SUMMER_MAX"


@pytest.fixture
def scoring_store(tmp_path):
    """Four records: «الطالب» in three of them, «الحرمان» in exactly one.

    Sized so the common word's normalised IDF is log(4/3)/log(4) = 0.21 — nonzero
    but under the floor. That gap is what separates the two failure modes: a raw
    overlap COUNT scores it 1.0 and lets it through, and dropping the floor lets
    anything above zero through. A word appearing in every record would score a
    clean 0.0 and both mistakes would look correct.
    """
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump({"authority_precedence": [], "sources": []}), encoding="utf-8"
    )
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(
            [
                _record("P.ONE", "t1", source_text_ar="الطالب يسجل مقرراته."),
                _record("P.TWO", "t1", source_text_ar="الطالب يراجع خطته."),
                _record("P.THREE", "t1", source_text_ar="الطالب يقابل مرشده."),
                _record("P.RARE", "t2", source_text_ar="الحرمان يمنع دخول الاختبار."),
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return PolicyStore.load(tmp_path)


def test_a_word_common_to_most_records_does_not_qualify_any_of_them(scoring_store):
    """Every Arabic sentence shares some filler with some record; that is not a match.

    Without this bar an out-of-scope question always matched something, and the
    matches were whichever records happened to be longest.
    """
    assert scoring_store.lookup(query="الطالب", as_of=TODAY)["policies"] == []


def test_a_rare_word_does_qualify_its_record(scoring_store):
    found = [p["policy_id"] for p in scoring_store.lookup(query="الحرمان", as_of=TODAY)["policies"]]
    assert found == ["P.RARE"]


def test_a_rare_word_outranks_a_common_one(scoring_store):
    """Distinctiveness decides the order, not how many words happen to coincide."""
    found = [
        p["policy_id"]
        for p in scoring_store.lookup(query="الطالب الحرمان", as_of=TODAY, limit=10)["policies"]
    ]
    assert found[0] == "P.RARE"


def test_common_word_weight_is_below_the_floor_and_rare_word_above(scoring_store):
    common = max(scoring_store._idf.get(t, 0.0) for t in expand_tokens("الطالب"))
    rare = max(scoring_store._idf.get(t, 0.0) for t in expand_tokens("الحرمان"))
    assert 0 < common < MIN_LEXICAL_WEIGHT <= rare


def test_idf_is_corpus_size_independent():
    """Normalised to [0, 1], so the floor means the same thing in any store.

    Raw IDF scales with log(N): a threshold tuned against 81 records rejects
    everything in a store of 5, which is how the synthetic fixtures first broke.
    """
    store = get_policy_store()
    assert all(0.0 <= w <= 1.0 for w in store._idf.values())
    assert max(store._idf.values()) == pytest.approx(1.0)


def test_retrieval_is_deterministic():
    """The claim the whole measurement rests on: same question, same records.

    Float summation over a set iterates in hash order, which varies per process
    unless the terms are summed in a fixed order — enough to reorder near-ties.
    """
    store = get_policy_store()
    question = "كم مرة أقدر أنسحب من مادة؟"
    first = [p["policy_id"] for p in store.lookup(query=question, limit=10)["policies"]]
    for _ in range(5):
        assert [p["policy_id"] for p in store.lookup(query=question, limit=10)["policies"]] == first


# ── load-time integrity ──────────────────────────────────────────


def _write_store(tmp_path, records, name="rules.yaml"):
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump({"authority_precedence": [], "sources": []}), encoding="utf-8"
    )
    (tmp_path / name).write_text(yaml.safe_dump(records, allow_unicode=True), encoding="utf-8")


def test_duplicate_policy_id_is_rejected_at_load(tmp_path):
    """Two records, one id, is an unapproved-content leak waiting to happen.

    The approval gate tests an id against the set of approved ids while the BODY came
    from a plain by-id dict built last-write-wins over rglob order. So an unapproved
    redraft in a later-sorting file passed the gate and supplied the text — and
    withheld_unapproved_policy_ids reported nothing, because an approved record with
    that id did exist. Redrafting into a second file is the normal editing motion for
    a YAML store, so this has to fail loudly rather than be caught downstream.
    """
    _write_store(tmp_path, [_record("TU.X.Y", "t1")], name="a_approved.yaml")
    (tmp_path / "z_draft.yaml").write_text(
        yaml.safe_dump(
            [_record("TU.X.Y", "t1", status="EXTRACTED", source_text_ar="unapproved draft")],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate policy_id"):
        PolicyStore.load(tmp_path)


def test_unknown_decision_use_value_is_rejected_at_load(tmp_path):
    """A fifth value would reach the model with no instruction attached.

    The answer contract explains exactly four. An unexplained authority label is
    worse than a missing one: the model reads it as permission.
    """
    _write_store(tmp_path, [_record("TU.X.Y", "t1", runtime_use="PROBABLY_FINE")])
    with pytest.raises(ValueError, match="Unknown runtime_use"):
        PolicyStore.load(tmp_path)


def test_every_decision_use_in_the_real_store_is_one_the_contract_explains():
    store = get_policy_store()
    used = {r.get("runtime_use") for r in store.records if r.get("runtime_use")}
    assert used <= DECISION_USE_VALUES, f"undocumented decision_use: {used - DECISION_USE_VALUES}"


def test_the_prohibited_count_the_docs_quote_is_the_real_one():
    """The docs said 26 for a while. 26 is the number of records carrying ANY
    runtime_use value; 20 carry this one. A wrong number in an authoritative
    document is a defect, and this pins it to the data."""
    store = get_policy_store()
    prohibited = [r for r in store.records if r.get("runtime_use") == "PROHIBITED_FOR_DECISION"]
    assert len(prohibited) == 20
    assert len([r for r in store.records if r.get("runtime_use")]) == 26


# ── the retrieval defects the review found ───────────────────────


def test_stopwords_actually_filter():
    """They are written على/إلى/أن/أو but compared after folding to علي/الي/ان/او.

    Stored unfolded, those four never matched, so four of the commonest words in
    Arabic entered every token set — diluting IDF and able to carry a match alone.
    """
    from core.services.arabic_text import STOPWORDS, content_tokens, normalise

    assert [w for w in STOPWORDS if normalise(w) != w] == []
    tokens = content_tokens("هل يحق لي أن أعتذر على الفصل أو إلى الترم")
    assert not ({"علي", "الي", "ان", "او"} & tokens)


def test_asking_when_term_starts_does_not_retrieve_expulsion_rules():
    """«الفصل» is the semester everywhere except dismissal, where it is expulsion.

    The alias «الفصل من الجامعة» needed only فصل + جامعة, so «متى يبدأ الفصل الدراسي
    في الجامعة؟» routed to academic_dismissal — a timetable question answered with
    the rules for being thrown out.
    """
    store = get_policy_store()
    topics = {t for t, _ in store.resolve_topics("متى يبدأ الفصل الدراسي في الجامعة؟")}
    assert "academic_dismissal" not in topics
    # And the real dismissal question still routes.
    assert "academic_dismissal" in {
        t for t, _ in store.resolve_topics("معدلي نازل، هل يفصلوني من الجامعة؟")
    }


def test_only_the_strongest_topic_bypasses_the_lexical_floor(scoring_store, monkeypatch):
    """A weak topic hit must not admit records that share nothing with the question.

    Every topic whose aliases fired used to be admitted at the top rank tier, so
    records with zero lexical overlap displaced genuine matches.
    """
    monkeypatch.setitem(scoring_store._alias_tokens, "t1", [[expand_tokens("الطالب")]])
    monkeypatch.setitem(
        scoring_store._alias_tokens, "t2", [[expand_tokens("الحرمان")], [expand_tokens("الاختبار")]]
    )
    hits = scoring_store.resolve_topics("الطالب الحرمان الاختبار")
    assert hits[0][0] == "t2", "t2 matches two aliases, t1 one"
    assert dict(hits)["t1"] < dict(hits)["t2"], "t1 must be the weaker hit"

    found = {
        p["policy_id"]
        for p in scoring_store.lookup(query="الطالب الحرمان الاختبار", limit=10, as_of=TODAY)[
            "policies"
        ]
    }
    # P.ONE/TWO/THREE share only «الطالب», whose weight (0.21) is under the floor.
    # Asserting the FIRST result is P.RARE is not enough — it stays first either way.
    # What must not happen is the weaker topic dragging its whole membership in.
    assert found == {"P.RARE"}, f"a weaker topic admitted below-floor records: {found}"
