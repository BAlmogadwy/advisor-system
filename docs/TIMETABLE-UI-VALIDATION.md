# Timetable workspace UI validation

Local development validation on 2026-10-10. Changes cover the classic workspace,
the shared planner card, and the MRI, Graph and Split handoffs. The existing
generation engines, optimisation modes, manual editing, locks, slot editing,
repair flows, exports and published-scenario protection remain available.

## Corrections

- Repaired the malformed Publish button. Added native menu buttons, keyboard
  navigation, accessible tab state, focus restoration and an operable Details
  disclosure. Responsive layouts retain the boards, grid and inspector panes;
  menus stay within the mobile viewport.
- Unified planner ownership across the main toolbar and background-run card.
  Submission immediately protects conflicting writes, reload discovers active
  jobs, and terminal completion refreshes the captured scenario before restoring
  editing. Failed polling remains visibly unconfirmed and continues monitoring.
  A server-side scenario guard also protects manual edits, publish and repair
  application against concurrent planner writes from another browser.
- Corrected Undo/Redo to preserve the original action, times, section metadata
  and newly created row IDs through repeated cycles. Failed requests retain
  history. Saved manual boards expose eligible sections, with search and a
  Show more control beyond the first 100 results.
- Guarded scenario and board loading against old responses and failed selection.
  MRI, Graph and Split return links preserve the available scenario/board scope.
  Repaired corrupt Arabic interface strings in Split.
- Added a read-only canonical quality summary. Instructor days and idle use
  physical meeting intervals; soft seats are unresolved course enrolments.
  Physical room suitability includes missing, undersized, incompatible and
  double-booked rooms with existing capacity buffers. Shared meetings count
  once, and remote meetings do not require physical rooms. Course-demand
  overlaps are labelled separately from evaluated assignment clashes.
- Corrected accepted running-job cancellation: a safe solver boundary is followed
  by atomic restoration before the job becomes cancelled. V2 checks between
  major phases, scheduler checks progress boundaries, and Classic checks after
  its call returns. An active solver/persistence call finishes before the next
  check. Live workers retain ownership beyond the stale timeout, and a worker
  that loses its claim cannot restore over later edits. Restoration preserves
  section/placement/meeting identities, locks, student assignment sources,
  programme and board links, repair references and grid configuration, including
  historical external-board dependants of the scenario's sections. Failure
  to restore produces an explicit failure rather than a false cancellation.

## Browser method and real-data observations

Chrome tests used an isolated SQLite backup of the user's actual timetable data.
Generation, optimisation cancellation and manual edit/history exercises ran on
that copy. The original saved timetable was not overwritten by browser tests.

The saved AI/AI2/DS/DS2 term-1 scenario 663 displayed 18 instructor days, 780 idle
minutes, 18 unresolved soft-course enrolments and 3 physical meetings needing a
suitable room. Term 7 displayed 23 meetings and 2 room shortages: FE2 missing a
room with required buffered capacity 61, and CS323 in an undersized room with
required capacity 26. The third shortage was PHYS103 in Term 1. Full MRI counted
the same three physical shortages across the scenario.

Desktop and responsive widths 1024, 768 and 390 pixels were inspected. The
mobile generator menu stayed within the viewport; all three workspace panes
remained accessible. Keyboard menu selection/Escape and inspector tabs worked.
Browser lock/unlock and Remove/Undo/Redo/Undo restored the selected board's
23 meetings and original quality totals. A real Classic generation completed
and restored controls. The combined XLSX export returned HTTP 200 with a
46,379-byte workbook.

Two running Optimise Current cancellations completed with controls restored.
The final checkpoint run (job `13122abe-6254-4d79-b28f-7e51ecf0d032`) finished
cancelled after its local-search phase. Every captured snapshot field exactly
matched the pre-job baseline: 44 sections, 100 placements, 100 meetings, row
identities, timestamps, locks and grid configuration. The real scenario had no
student assignment/programme/repair rows in those snapshot collections; unit
fixtures additionally verified those populated dependencies and historical
external-board references. Instructor totals remained 18 days and 780 idle
minutes, with 18 unresolved soft enrolments and 3 room shortages.

Cancellation remains cooperative. The default V2 local-search budget is 180
seconds, CP-SAT defaults to 60 seconds, and chain search is iteration-limited.
There is no fixed wall-clock cancellation promise.

The Graph preview and navigation remain available locally. Neo4j was not
configured in this environment; external synchronization was not exercised.

## Validation scope

Focused DOM tests execute the production scripts against the real workspace
markup, including combined main/card/accessibility ownership. Backend tests
cover canonical quality, active-job write guards and cancellation/rollback.
The final broad timetable/scheduler/instructor/planner regression command passed
**1,120 tests with 3 skips** in 111.57 seconds. The skips are the existing PR3
instructor-source trace sentinel, a PR5 case where simulated annealing leaves
nothing for CP-SAT to override, and a PostgreSQL-specific row-lock test on SQLite.
Focused cancellation/ownership tests and production-script DOM wrappers also
passed. Ruff, JavaScript syntax checks, targeted new-service type checks and
Django system checks passed.

The scheduler minimum-demand regression now verifies the actual async call:
five students by default, and an explicitly requested threshold of three. It
does not depend on where the runner's implementation text is located.

```powershell
$env:PYTHONPATH="$pwd;$pwd/tests"
$timetableTestFiles=@(rg --files tests -g 'test_timetable*.py' -g 'test_instructor*.py' -g 'test_scheduler*.py' -g 'test_pr[1-8]*.py' -g 'test_planner_job*.py')
.venv\Scripts\python.exe -m pytest @timetableTestFiles -o addopts='' -q -rs
```

Local browser proof is saved under ignored `.tmp/timetable-quality/ui-fix/`:
`final-desktop.jpg`, `final-mobile.jpg`, `final-cancelled.jpg`, and
`browser-cancel-final-verified.txt`. Temporary responsive overrides were reset.
The main server remains running at `http://127.0.0.1:8001`; the isolated test
server was stopped after all its jobs finished.

This is a local implementation and validation. No production deployment or
production release approval gate was performed.
