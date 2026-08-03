# Phase 1 — the policy store, wired

**The adviser can now quote the university's written rules, and can only quote the
ones it actually looked up.**

Before this, `policies/` held 81 approved rule records that no line of runtime code
read. The adviser answered regulation questions the only way it could: from the
model's own memory of *some* university's rules. This wires the store in and puts a
check between the model and the citation.

| | before | after |
|---|---|---|
| capabilities | 17 | 18 (`policy_lookup`) |
| runtime references to `policies/` | 0 | the store is the only route |
| policy records reachable by the adviser | 0 | 81, all `AUTHORITY_APPROVED` |
| citation machinery | none | structured, validated, retry on fabrication |
| tests | — | 77 new, 31 of 32 mutants caught |

---

## What `policy_lookup` returns

Student-accessible. Touches no student data — the rules are the same for everyone —
so there is no scope resolution to do; the registry's role check still gates it like
every other capability.

```python
registry.execute("policy_lookup", {"query": "كم مرة أقدر أنسحب من مادة؟"},
                 scope={"role": ROLE_STUDENT})
```

Each policy carries the ten fields the answer contract requires: `policy_id`,
`topic`, `title_ar`, the operative `rule` block, `exceptions`, `source`
(document, page, edition), `authority` (level, precedence rank, approval state and
approver), `effective` (from, to, currentness, expired) and `citation`.

Two fields do the safety work and are easy to skim past.

**`decision_use`** is the record's own statement about whether it may be applied to
a student, surfaced verbatim from `runtime_use`. 20 of the 81 records are
`PROHIBITED_FOR_DECISION` — the inputs their conditions need do not exist in the
schema. There is no warning-count feed, so `TU.DISMISSAL.THREE_WARNINGS` can be
explained and never evaluated. For those records, explaining the rule *is* the
complete answer and ruling on the student's case is the failure.

**`citable`** is the exact set of citations permitted for this request. Anything
else is rejected downstream.

## The citation contract

The prompt mandates one machine-checkable form:

    «الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»

The bracketed id is what makes a citation checkable, and the first version of this
contract did not have it. It asked for «... الإصدار الثالث، ص NN» — document, edition
and page, no id — while the only enforcement scanned for dotted upper-case ids. The
two never intersected: an answer that FOLLOWED the instruction contained no id, so
the fabrication list was always empty, the retry never fired, `cited_policy_ids` was
always `[]`, and the page — the one thing a student can check against the printed
guide — was validated nowhere. A five-lens review found it. It is fixed, and now
tested from both directions.

`validate_citations` rejects on six grounds — `UNKNOWN_POLICY`, `NOT_APPROVED`,
`NOT_RETRIEVED_THIS_REQUEST`, `EXPIRED`, `PAGE_NOT_IN_RECORD`, `EDITION_MISMATCH` —
and the loop adds a seventh for a bare «ص NN» belonging to no retrieved policy. It is
called from the request path now; previously it had no production caller at all, so
page, document and edition were checked only by its own tests.

`NOT_RETRIEVED_THIS_REQUEST` matters most: a real, approved, current policy is still
rejected if this request never fetched it, otherwise an id recalled from training
reads as grounded.

On a bad citation the loop re-asks **with the retrieved policy text in the correction
message**, not just the permitted ids — asking a model to rewrite a rule answer while
showing it only a list of ids leaves it nothing to rewrite from except its own
memory, which is the failure the feature exists to prevent. The retry is kept only if
it is no worse than the draft. **If the citations still do not check out, the answer
is not returned at all** — the student gets an explicit "I could not verify this" and
a pointer to the Deanship, in their own language. Shipping an answer the system has
already determined is misattributed, wearing a real-looking citation, is the most
dangerous output this system can produce.

The response carries `citations` (what the answer was entitled to cite) and
`cited_policy_ids` (what it did), so a judge or a UI can check one against the other
without parsing prose.

## What a student may see

`decision_use` reaches everyone. The engineering annotations behind it do not.
`runtime_use_reason`, `runtime_use_note`, `never_infer`, `open_question` and `notes`
name database tables, quote row counts, and quote counts of students by status — a
student asking about their GPA was being handed the schema and their peers'
statistics. They are staff-only now, defaulting to withheld so that forgetting the
flag fails closed. Where a record carries an `open_question` the student is still
told the source is unclear on that point; what they no longer get is the note
explaining which database column is empty.

## Conflicts

`sources.yaml` declares one: the guide says registration changes close a week
before teaching starts, the 1448-T1 calendar shows add/drop running until the day
before. Both records are returned, each carrying the resolution and which side it
is on. The calendar governs because `OFFICIAL_ACADEMIC_CALENDAR` outranks
`OFFICIAL_STUDENT_GUIDE`. Neither is silently dropped, and the prompt forbids
presenting them as equally valid or averaging them.

## Retrieval: topic-keyed, deterministic, no embeddings

Three signals, in priority order: a curated Arabic alias table
(`policies/topic_aliases.yaml`) routing a question to one of 27 topics; IDF-weighted
token overlap against the whole record; then authority precedence. Same question,
same records, every time.

Four things were wrong on the first pass and are worth recording, because each was
invisible until measured:

**Only a third of the store was indexed.** Indexing `title_ar` + `source_text_ar`
reaches 3,119 of the 8,220 tokens the records actually contain. 11 records have no `source_text_ar` at all
and keep their whole content in structured fields — the terminology definitions, the
grade table, the اعتذار/تأجيل/انسحاب comparison — so they were reachable only by
their four-word titles. Those are exactly the records the eval set leans on hardest.

**Aliases were matched as substrings.** «الاعتذار عن الفصل» could not match a
student writing «أعتذر عن الفصل»: same stem, no shared literal span. Matching each
alias word against the query's token variants doubled topic routing, from 76 to 157
of the expected pairs.

**The IDF floor was corpus-dependent.** Raw IDF scales with `log(N)` — 4.4 across 81
records, 1.6 across 5 — so an absolute threshold tuned on the real store rejected
everything in the test fixtures. Normalising to `[0, 1]` makes the floor mean the
same thing in any store.

**Length normalisation was tried and rejected.** Dividing by `sqrt(record length)`
lowered recall measurably. The longer records genuinely are more often the right
answer; they contain more rule content. Kept out on the evidence.

Three more came out of the review:

**A topic hit bypassed the relevance floor without limit.** `resolve_topics` returns
every topic whose aliases fired and all of them were admitted at the top rank tier,
so records sharing nothing with the question displaced genuine lexical matches in 18
of 252 eval questions. Only the strongest topic bypasses the floor now.

**Four of the commonest Arabic words never filtered.** The stopword list is written
على/إلى/أن/أو but compared after folding, where they are علي/الي/ان/او. Stored
unfolded they matched nothing, entered every token set, and diluted the IDF
denominator.

**«الفصل» is the semester everywhere except dismissal, where it is expulsion.** The
alias «الفصل من الجامعة» needed only فصل + جامعة, so *«متى يبدأ الفصل الدراسي في
الجامعة؟»* — when does term start — routed to `academic_dismissal` and answered a
timetable question with the rules for being thrown out. That alias is gone; the
unambiguous verb forms carry the topic.

### Retrieval cannot be the abstention mechanism

The obvious design — tighten the relevance floor until out-of-scope questions match
nothing — was built, measured, and removed.

It does not work here, and the reason is worth stating. The questions with no
answering policy are not off-topic. «تأخري عن المحاضرة ينحسب غياب؟», «كم مرة مسموح
أعيد نفس المادة؟», «هل لازم أسجل المحاضرة والمعمل بنفس رقم الشعبة؟» are squarely
within the store's subject matter; the obtained sources simply do not state the rule
(`NO_POLICY_SOURCE`). Their neighbouring records *should* be retrieved. Tightening
the threshold until they returned nothing cost 95 of 252 genuine answers and still
caught only 6 of 14 targets.

So abstention is an answer-contract duty, not a retrieval one, and the prompt states
it directly: retrieval returns the neighbourhood of a question, not proof the answer
is in it. Stretching the nearest policy to cover a gap produces a fabrication with a
real citation attached — worse than an obvious one, because it survives checking.

## What the numbers are

Measured by `evals/advisor/policy_recall.py` against the `policy_ids` the eval set
already carried, for the 252 of 284 questions that name at least one.

```
                                       first pass      now
policy resolution recall (all pairs)        0.264     0.493   <- the gate
expected policies found                       170       318   (of 645)
policy precision                            0.145     0.177
complete-set accuracy                       0.163     0.325
```

**Do not gate on the "reachable population" figure.** An earlier version of this
document led with it (0.548 -> 0.693) as though it measured retrieval independently.
It does not. A pair is classified unreachable by exactly the condition `lookup()`
uses to reject a candidate, so every retrieved pair is reachable by construction and
the number is identically `recall_all / ceiling`. Because the retriever computes its
own denominator, weakening it shrinks both halves together: reverting to the
pre-commit indexing drops all-pairs recall 0.498 -> 0.425 while the reachable figure
moves 0.693 -> 0.692. A floor on it would not have noticed. The gate is on all-pairs
recall and the absolute count of policies found.

**The headline number is bounded at 0.718 and cannot reach the 0.95 target**, for a
reason that is about the ground truth rather than the retriever. 28% of expected
(question, policy) pairs have no signal connecting them — no shared token, no alias
— because the expected sets mix two different relations: the policy that *answers*
the question, and standing advice that should *frame* any answer. Seven policies
spanning six to twelve categories supply a third of all pairs;
`TU.CONTACT.ADVISER_CHANNELS` ("talk to your adviser") is attached across 12 of 16
categories. No retriever recovers "your choices are final" from "what courses do you
advise me to register" — embeddings included, because the connection is editorial,
not semantic.

The recommended fix is to split the relation in `expected.yaml` into `required` and
`supporting` policy ids, and measure recall against `required`. That is an owner
decision, not a change to make while being graded by it: relabelling to raise one's
own score is the thing criterion 12 exists to prevent. Until then the harness reports
both numbers and `--floor` guards the all-pairs one.

## Known gaps

- **Nothing measures whether the model complies.** The fabrication check is
  enforced, but end-to-end behaviour against the 284 questions needs the judge
  harness, which is Phase 5.
- **All 81 records are `currentness_status: UNVERIFIED`** and none carries an
  effective date. Expiry logic is implemented and tested against fixtures; it has
  never fired against real data because no real record has an end date.
- **`sorted()` on the weight summation is defensive and untested.** Float addition
  is order-dependent and set iteration order varies across processes. Three
  hash seeds produced identical rankings, so the mutation that removes it survives.
  It is cheap insurance, not a covered guarantee.
- **The single-shot fallback path has no policy contract.** When the agent loop is
  disabled or the model rejects tool calling, `SYSTEM_PROMPT` is used, it carries none
  of the rules above, and the seed planner never calls `policy_lookup`. Regulation
  questions on that path are answered from model memory exactly as before this commit,
  and the citation check raises nothing because nothing was retrieved. This is the
  largest remaining hole and it is not fixed here.
- **The advisor model is not pinned.** `LOCAL_LLM_MODEL` is empty, so
  `resolve_model()` takes whatever LM Studio lists first — `qwen3.6-35b-a3b` today.
  Eval numbers are reproducible only while that ordering holds.
- **The 8 `open_question` and 5 `contested` markers survive approval unchanged**, as
  they did before wiring. Approval was the owner acting as authority for this
  system's own use; it is not a statement that the Deanship reviewed these records.
