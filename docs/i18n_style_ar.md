# Arabic Style Guide for Long UI Sentences

## Goal
Translate long English UI/help/warning text into clear, natural Arabic while preserving exact intent.

## Method (apply in order)
1. **Intent First**: determine if sentence is instruction, warning, info, or status.
2. **Clause Split**: break long English sentence into 2–3 logical Arabic clauses.
3. **Natural Rebuild**: reorder naturally for Arabic readability.
4. **Glossary Lock**: enforce approved terminology from `i18n_glossary_ar.md`.
5. **UI Fit Check**: shorten if the text is too long for card/button/toast areas.

## Tone Rules
- Buttons: concise action verbs.
- Warnings: explicit and safety-first.
- Help text: short, practical, non-academic.
- Status/meta lines: compact and scannable.

## Good Patterns
- "Run preview, inspect top courses, and export aggregate CSV."
  → "شغّل المعاينة، وافحص أعلى المقررات، ثم صدّر CSV تجميعيًا."

- "Always run preview first. Destructive actions require explicit typed confirmations in popups."
  → "ابدأ دائمًا بالمعاينة أولًا. العمليات الحساسة تتطلب تأكيدًا كتابيًا صريحًا في النوافذ المنبثقة."

## QA Checklist (Track 7)
- No mojibake/garbled Arabic.
- No empty Arabic branch in bilingual conditionals.
- No accidental English leftovers in Arabic branch.
- Consistent terminology.
- Reads naturally in RTL context.
