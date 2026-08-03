# Elective mapping publication — plan

Read-only. **No rows are published by this document or by the commit that adds
it**, and none may be published until an authoritative source is attached to §3.

The course-detail screen already renders elective options the moment a
programme-slot-term goes `READY` (PR #57). Nothing further is needed on the screen
side. What is missing is the **data**, and the only honest way to add it is to know
where it came from.

---

## 1. What exists today

Measured on the development database, 2026-08-03.

**Programmes with students** — four, 320 students:

| programme | students |
|---|---|
| AI | 117 |
| AI2 | 66 |
| DS | 88 |
| DS2 | 49 |

**Elective slots**: 84 declared across 12 programmes; **28 in the four programmes
that have students**.

**`ElectiveTermMapping` rows — the only publication record**: 23, all
`1448/term 1`.

| programme | slot | options |
|---|---|---|
| AI | AI1 | 1 |
| DS | DS2 | 1 |
| CS | CS1 | 3 |
| CS | CS2 | 3 |
| IS | IS1 | 5 |
| IS | IS2 | 5 |
| IS | IS3 | 5 |

**Twenty-one of the twenty-three rows serve CS and IS, which have no students.**
Two rows serve a live programme, one option each. So the publication effort is
essentially untouched for the entire live population.

---

## 2. Two blocking data facts

Both were found by measurement, and both change what can be published at all.

### 2.1 Sixteen of the 28 live slots cannot be satisfied by any catalogued course

Every course in `ElectiveCourse` is **3 credit hours**. Live slots require:

| required hours | slots | types |
|---|---|---|
| 3 | 12 | Program Elective |
| 2 | **16** | Free Elective, University Elective |

There is **no 2-hour course in the elective catalogue at all**. Under the credit
compatibility rule (§5), every Free Elective and University Elective slot in every
live programme would be rejected on import.

That is not an importer problem to work around. It is one of:

* the slot credit values are wrong in `ProgrammeRequirement`;
* the catalogue is missing the 2-hour courses these slots are meant to offer;
* Free/University electives are approved from a different source entirely and do
  not belong in `ElectiveCourse`.

**Owner decision required before any FE/GSE mapping is written.** Publishing a
3-hour course against a 2-hour slot would let a student choose an option that does
not satisfy the requirement they chose it for.

### 2.2 AI2 and DS2 have no catalogue of their own

`ElectiveCourse.programme` holds `AI`, `CS`, `CYP`, `DS` and `''`. AI2 and DS2 —
**115 students between them** — have no entries.

Either they draw on the base programme's catalogue (`AI2` → `AI`), which is a
**cross-programme mapping and needs the explicit approval §5 demands**, or their
catalogue has not been entered. The offset plans are not simply the base plans:
`CS111` is already known to be two different courses across offset plans, so
inheritance cannot be assumed.

The five `programme=''` rows are a third, separate gap.

---

## 3. The authoritative source — NOT YET IDENTIFIED

**This section is deliberately empty of content and blocks everything below it.**

For each `(programme, slot, term)` the publication must record:

| question | why it cannot be inferred |
|---|---|
| which concrete courses are approved | membership in `ElectiveCourse` proves the course is *catalogued*, not that it is *published for this slot this term* |
| where the approval came from | a mapping with no provenance cannot be audited, corrected or defended |
| does it apply to one term or persist | `ElectiveTermMapping` is term-scoped; a mapping that should persist must be re-published per term, deliberately |
| may a student take more than one from the slot | affects whether the screen offers a list or a single choice |
| expected credit value | see §2.1 |
| retain / add / replace / reject the existing row | the two live rows already exist and were not published by this process |
| which active students are affected | 117 / 66 / 88 / 49 per programme |

### Forbidden inference

A mapping must **never** be derived from:

* course-code prefixes — this is issue #55 in another costume, and it cost seven
  mandatory courses their correct classification
* course names
* `ElectiveCourse` membership alone — §3 above
* a mapping from another term
* what looks academically plausible

The last is the dangerous one, because it is the one that feels like diligence.

---

## 4. Publication mechanism

A controlled import from a versioned file. Not hand-written ORM edits, and **not a
data migration** — a migration makes changing an approved academic mapping look
like changing application structure, and it is the wrong place for term-specific
administrative data.

```csv
academic_year,term,programme,slot_code,course_code,source_reference
1448,1,AI,FE1,AI463,approved-plan-1448
1448,1,AI,FE1,AI464,approved-plan-1448
```

`source_reference` is not decoration. It is the answer to "who approved this?", and
a row without one cannot be published.

```bash
python manage.py import_elective_mappings mappings.csv --dry-run
python manage.py import_elective_mappings mappings.csv --apply
```

The command must:

1. parse and normalise every identifier (`normalize_code`, the shared one)
2. validate the **whole file** before writing anything
3. produce a deterministic diff — add / retain / replace / reject, per row
4. write atomically
5. refuse duplicates and contradictions
6. be idempotent — a re-import of the same file writes nothing
7. record enough to reverse the publication

Dry-run is the default, as with every destructive command in this project.

---

## 5. Validation — reject the whole import if any row fails

* programme does not exist
* slot is not declared an elective requirement **by its `type`** (`is_elective_slot`)
* course is not in the elective catalogue
* course belongs to another programme with no explicit approved cross-programme rule
* year or term missing or invalid
* duplicate `(programme, slot, course, year, term)`
* conflicting rows for the same logical mapping
* credit hours conflict with the slot's requirement — see §2.1
* the import would silently delete a mapping not present in the file

**Deletion and replacement are never inferred from omission.** They require
`--replace-year 1448 --replace-term 1`, and that mode reports exactly what it will
remove before removing it.

---

## 6. Activation contract

Readiness is per `(programme, slot, term)` — never per programme:

```
explicit mapping exists
+ every referenced option validates
+ no mapping conflicts
+ at least one option resolves
= READY
```

So a programme may legitimately sit in a mixed state:

```
AI / FE1 / 1448-1 -> READY
AI / FE2 / 1448-1 -> NOT_PUBLISHED
```

and the student page must expose options for the first and the standard
not-published sentence for the second. `slot_status` is already keyed this way; no
screen change is required.

---

## 7. Tests the mapping branch must carry

* a dry run performs zero writes
* invalid input is all-or-nothing
* an identical re-import creates no duplicate rows
* a mapping for another term does not open the gate
* a mapping for another programme does not open the gate
* one ready slot does not activate its sibling slots
* replacing a term requires explicit authorisation
* readiness flips false → true only after a successful commit
* a failed validation or rollback leaves the prior ready state unchanged
* the student JSON still exposes only `mapping_ready`, never an operational state

---

## 8. Review checklist — the dimensions mutants do not cover

Seven defects reached review in PR #57 despite every rule being mutation-audited.
They were not missed because the tests were weak in the ordinary sense; they were
missed because the audits asked "is this rule enforced?" and never "is this the
right rule, in the right place, said the right way?".

Check each explicitly, on this branch and on the import:

| dimension | the question | how it failed before |
|---|---|---|
| **claim semantics** | does any string assert something the system cannot stand behind? | «تستطيع تسجيله الآن» in a status badge — a registration permission |
| **student-visible leakage** | does the payload reveal what the wording hides? | `mapping_status: INVALID_MAPPING` beside the sentence written to hide it |
| **route response type** | does an HTML route ever answer with JSON? | the throttled page, then the throttled form POST |
| **rate-limit truthfulness** | is the retry time the limiter's, or invented? | `Retry-After: 60` against a 600-second window |
| **query placement** | is the expensive call inside the branch that reads it? | `build_unlock_report` ran before classification |
| **common-path cost** | is the most frequent answer the cheapest? | unmapped elective — 77 of 84 slots — cost 131 queries to say nothing |
| **display completeness** | does every field that reaches the screen have a value? | `unlocks` rendered every course name empty |

For the import specifically, the same lens: does a rejection message reveal more
than an operator needs? Is the diff read before the write, or after? Does the
common case — a file that changes nothing — cost the same as one that changes
everything?

---

## 9. Sequence

```
authoritative mapping inventory   <- BLOCKED on §3
-> dry-run importer and validation
-> reviewed publication data
-> atomic apply
-> readiness verification
```

Steps 2 and 3 of the importer can be built against fixtures while §3 is open. **No
elective row may be written to the live database until §3 names its source.**
