# Exam page interaction tests

With the Python virtual environment activated:

```sh
npm ci
python -m pytest tests/test_exam_frontend_interactions.py
```

The pytest wrapper renders the current Django template in English and Arabic,
then uses Node's built-in test runner and jsdom to execute the complete exam
page script. Requests use explicit fixtures; no server, database, browser, or
network is accessed. Unexpected requests and JavaScript errors fail the tests.

Coverage includes course search and visibility, visible-only and global selection
actions, canonical course identities, summary and empty states, period/day edits,
fixed exams, loaded-run guards, and keyboard-accessible controls. Hidden course
rows remain selected unless an explicit selection action changes them.
The course-to-pin combobox is covered for normalized search, keyboard and pointer
selection, cancellation, uncommitted queries, canonical identity, and eligibility.
Manual editing tests separate movement, pinning, checking and optimization;
cover pinned drag locks, the Move dialog, Undo/Redo, same-slot no-ops, stale
cards/rooms/details, and five moves followed by one complete check. Deferred
responses exercise coalescing, revision guards and disabling live calculation.
Save tests verify exact placements and the reviewed input fingerprint, including
upstream-input rejection. History, filter and navigation guards protect drafts.
The compact workspace is checked for saved-baseline KPI comparisons, suppressed
comparisons when inputs change, unique identity-based placement counts, separate
pin changes, and baseline resets. Conflict, workload and room details exercise
exact-course Find/Move actions, retained search and focus, stale-report slot
labels, and unavailable/pinned/busy action guards. Checking keeps expanded
details open; only a newly loaded or saved result collapses setup and summary.
The full-height schedule has native row/column table headers that scroll with
the page. Tests cover deep scroll retention (including negative RTL offsets),
Find/Move positioning, and compact two-word course names with accessible icon actions.
Display-only abbreviation skips copied numeric prefixes while preserving Arabic,
3D/C++ word tokens, full names and canonical course identity.
Full names and calculation metadata remain intact in requests while credits and
room labels stay outside the compact grid view.
Live-check tests also preserve the visible workspace's page position through
loading-status reflow and open-detail shrinkage, avoid navigating an offscreen workspace, and keep period-column
sizing synchronized with evaluated settings.

The complete control audit also covers:

| Screen area | Interaction checks |
| --- | --- |
| Setup and scope | Required values, numeric bounds, every period operation, empty programs/sections, canceled and confirmed scope changes |
| Course selection | Text/visibility search, visible-only and global presets, online selection, preset menu dismissal |
| Policy | Shuffle payload, tiny-course toggle, exact 1–10 threshold validation on Build/Check/Save/Optimize |
| Fixed times and editing | Searchable identity selection, pin/edit/remove/both Unpin all controls, drag, Move confirm/cancel/Escape, Undo/Redo |
| Calculations and persistence | Live preference/coalescing, manual Check, source changes, exact Save, explicit Optimize, export guards |
| Summary details | Conflict, overload, heavy, same-day pairs, room, thin-course/clash and multi-sitting controls; exact Find/Move and unavailable-state guards; "—" (never 0), spoken "Not calculated" and a stated reason for a run saved before a metric existed, and a Check that first measures it leaves exports open; saved text printed literally; the styled clash mark checked against its CSS rule |
| Conflict matrix | Show/hide, mouse/keyboard exact-alias actions, literal labels, incomplete edges, zoom limits, repeated Fit, fullscreen Escape/focus/background restoration, scroll/focus retention |
| History | Load/discard guards, delete cancellation/confirmation, accessible page controls, out-of-order responses, failure/retry |
| Session recovery | Every exam endpoint handles login HTML without navigation or draft loss; visible sign-in recovery, rotated CSRF retry, Save pre-check error propagation, and network/server errors |

The shared confirmation dialog is also executed directly for canceled scope
changes and confirmed reload of the same run. Three live-response timings verify
that canceling a scope change adds no Undo step.
Teaching-section checks preserve official labels (including Arabic, slashes and
HTML-like text), separate room-group metadata, retain canonical course actions,
distinguish missing/ambiguous/unknown section data, and refresh the mapping-gap
notice without changing scope or placements. F/M remain student-group filters.
Actual-population checks cover imported-timetable source copy, empty source data,
pre-policy saved-run review/rebuild gates, and unavailable-course rejection for
Build, Check, Save and Optimize. Rejections preserve the draft and Undo history;
opening the recovery action never fetches or silently removes courses.

The separate `exam-review.test.cjs` suite loads the same production page and
review controller. It covers study-plan terms, mixed or missing mappings,
same-code course identities, and per-pair shared-student counts (including thin
courses). Visual review controls must preserve saved state, placements, pins,
search filters, and export availability without making requests. Manual edits,
Undo/Redo, Check and Save exercise exact focused-course retention and stale
snapshot labels. Missing or incompatible review metadata is unavailable rather
than a claim of zero shared students. Native review buttons remain separate from
the existing Pin, Move and double-click interactions.

The `exam-linked.test.cjs` suite covers linked exams (courses examined as one
exam) on the same page, in English and in Arabic: the builder section's
picker (selected courses only, never a course twice, chosen courses taken
back), the links in every Build, Check, Save, Optimize and Fix request by
identity (`[]` when there are none), links restored from a loaded run before
its saved signature (an unedited Check leaves Save off), and kept by identity
through Load Courses when display codes are renumbered. On the board, linked
cards sit in one labelled group, each naming its partners in a left-to-right
isolate; drag, the Move dialog, the pin toggle and the pin editor act on the
whole link, and one Undo step reverses it - a link pinned through one member
is pinned whole. Linking courses that sit apart opens the alignment dialog
(their current times in timetable order, the larger course's preselected; a
pinned course's time the only choice; courses pinned apart refused). A link
edit needs a Check before Save and has its own Review changes group. Server
refusals (`linked_exams_*`, with the field naming the link and member) are
worded by the page and shown beside the link, with a way there from the
banner; a split link from a saved run blocks Check until a move mends it.
The warnings from `qa.linked_exams` never block saving and say when they are
the last check's; the conflict detail names `linked_same_slot` clashes, Fix
says it cannot separate linked courses, and the review controller's
`relationship` knows a `linked` state. Chromium layout at 1366px and 375px, in
both languages and in a wide font, is covered by
`tests/test_exam_linked_browser.py`.

The `exam-student-export.test.cjs` suite runs the same page with the Student
data dialog (`static/js/exam-student-export.js`). It covers the button's
Department-files rule and its stated reason, the in-place link from Department
files, focus on open and back to the opener (the `browserFocus` model, which
here also treats a closed `<dialog>`, a disabled control and a disabled
fieldset as unfocusable), pickers from the saved run and then from the
preflight, "about" saved figures, debounced re-pricing with one preflight out
at a time (options changed meanwhile follow its answer; a failure for replaced
options asks again), the choices digest, waiting while another export holds
the server, counts shown only while priced with the current options (dimmed
while re-priced, cleared when they cannot be), the Matches/Changed/Refused/
unchecked check states announced once, a programme-only change advised to
update the saved program counts rather than resize rooms, programme shortcuts, groups new since
the save, exact request bodies, the blob download with the server's file name
and reference, inline errors with Try again (focus stays in the dialog), a
refused date named and marked on its input, Enter in a field, and a board
change under the open dialog. The real endpoints, a real workbook and Chromium
focus and layout are covered by `tests/test_exam_student_export_browser.py`.

The Student lists suites run through `tests/test_exam_rosters_frontend.py`,
which renders both pages with their real views (English, and Arabic when it
is asked for) and records every JSON answer from the real endpoints on the
export fixture's saved run, before and after the lists change under it (a
low-ID student joins a section and a new section appears, so an original
member loses a seat), plus a second saved run whose roomed section the lists
do not name; `roster-harness.cjs` serves those answers and fails a test on any
other request. `exam-rosters.test.cjs` covers the Student lists page: the
navigator by room (day tabs, period and group radios, Needs review, the
unroomed section and online rooms last under headings that name their unit -
sections, students, rooms - roving focus) and by course (period
groups, program, clashes, review, sorts), the audited POST per room or course
with focus kept in the navigator and Back returning to the previous room, the
address holding only the run, view, period and room or exam, section tabs
(eight, then More), flag chips as a radiogroup with good news when empty,
sorting, the ID or name filter, part groups naming rooms and ID ranges,
changed sections and No seat (worded as the file words it, and listed by
period apart from every room), batched long lists, Find (courses and rooms at
once, students by one POST after typing settles, never for a course or room
code, and an Enter pressed before the match arrives) and the student lookup
that never touches the address or history, Esc and Back with focus returned to
the navigator, the `/` shortcut and its switch, Refresh (one build in the
header, a newer build's navigator redrawing the open room's header; the list,
chips, filter and lookup kept), refusals, failed audits and
lost sessions in place, the back/forward cache, the master-detail screen below
800px (a room opened by its address goes back to the navigator in place, focus
on that room), and Export opening the phase-1 dialog preset to the room, course,
section or period on screen.
`exam-roster-drawer.test.cjs` covers the Timetable page: the view tabs, each
card's "N students" link (the saved count, first badge whatever the review
view paints, never dragged or pinned), the drawer's focus on open and back to
the link (found again after a re-render), the saved-run notice with unsaved
moves and where the draft has the exam, flag links opening the other exam
with Back, the Download menu's keys and its two one-click exports in the
remembered file language, More options preset, failed audits, the whole
run's check beside the exam's own changes, a text selection dragged onto the
backdrop, `?run=&focus=` from Student lists, and the same-day card on the
same page (its count and exam-pair detail from the real build, Find from a
pair landing on a grid card that keeps its link, the drawer leaving the
detail open; in Chromium too). Chromium layout (lists and
lookups inside the pane at 1600, 1280, 1024 and 700px in both languages, the
day grid of a five- or six-day week, headings and dates), focus after Back,
the real endpoints and a real download are covered by
`tests/test_exam_rosters_browser.py`.

Network behavior is tested with deterministic responses. These tests do not
create real saved runs, delete database rows, execute the optimizer, or inspect
downloaded workbooks; backend and export suites cover those contracts separately.

These tests cover controls and event wiring. They do not verify browser layout,
native dropdown appearance, or screen-reader announcements; those need Chrome
and accessibility review. The wrapper skips when Node or npm dependencies are
not installed, with an installation instruction in the skip reason.
