# Timetable Workspace UI — design record

Owner direction (2026-10-22): redesign the classic Timetable Workspace
(`/timetable-workspace/`) professionally — not quick fixes — and make every
style decision that is the same across the product in the **global** design
system rather than on this page only.

This record is the contract the implementation and its review are checked
against. It was written after a read-only audit of the page, the design system,
every page sharing its patterns, the tests that pin it and what each number on
it means, and revised after an adversarial design review and an engineering
risk review.

## 1. What was wrong (evidence, ranked by impact)

| # | Problem | Evidence |
|---|---|---|
| 1 | The page sits in a 1120 px column on any desktop; the grid pane is ~600 px and the last slot is always cut off | `.content-wrap{max-width:1120px}`; columns 220/1fr/290; `.tw-grid{min-width:650px}` |
| 2 | ~345 px of stacked bars before the first grid row; five nested scroll areas | header + Generate bar + command bar + quality strip; panes capped at 76vh |
| 3 | 72 % of the page's text is ≤ 11.5 px; secondary text (`--t4`) fails 4.5:1 in both themes; white on teal fails (4.11 light / 3.04 dark) | live count of 733 text nodes; computed ratios |
| 4 | Three button systems; split buttons render as two pills; buttons outside the command bar are unstyled browser grey | `.btn` `!important` geometry; `.tw-btn` scoped to `.tw-cmd-bar` |
| 5 | Inverted action hierarchy: Generate (used about once a term), the active board tab and Publish are all filled teal | `.btn-primary` / `.tw-btn-primary` |
| 6 | Publish is enabled while the server would refuse it; the refusal discards the blockers | client checks `critical` only; `check_publish_readiness` also blocks on rooms, missing sections, patterns |
| 7 | Numbers without meaning: board and scenario scope mixed, an empty instructor set shows a teal 0, the least important seat tier is the one painted red, at-risk students' seats are not shown at all | quality payload; tiered score |
| 8 | The same information two or three times: board list + board tabs; placed count ×3; status ×3; a hidden board strip; a bottom panel repeating the inspector | |
| 9 | Pastel cards in dark mode; colour carries no meaning; severity is a 1 px border plus a 7 px dot; room shortages never mark a card | `COURSE_COLORS`; `.has-issue::after` |
| 10 | The PR8 planner card has no CSS anywhere and renders below the fold as raw text — yet it holds the only Cancel for a running job | `pr8_async_job_card.html` |
| 11 | Year and Sem are shared by the scenario list and Generate, so preparing next term's generation clears the open scenario | `loadScenarios` → `resetWorkspace` |
| 12 | p-0 pages clip their own header (−1.5 rem bleed) | `.page-header` margin |

## 2. Principles

1. **A full-screen tool.** The workspace uses the whole width and height; the
   page does not scroll, its panes do (down to a 640 px tall window).
2. **One primary action.** Publish — and only when the server says the scenario
   is ready. Everything else is neutral; replacing actions are confirmed.
3. **Every number says scope, meaning and direction** in its visible label or
   group; unknown is "—" with a reason, never a coloured 0.
4. **One severity vocabulary** — critical / warning / info / ok — shown by icon,
   colour and text together.
5. **Readable by default.** Data text ≥ 12 px, micro-labels ≥ 11 px (floors in
   px, not rem, because the root size is fluid); text contrast ≥ 4.5:1, control
   edges ≥ 3:1, in both themes.
6. **Say it once.** No duplicate board lists, counters or status surfaces.
7. **Global first.** Built from the shared design system; page CSS holds only
   what is genuinely page-specific.
8. **Density rule for tool pages.** One glass band (the chrome); panes, cells
   and cards on solid surfaces; toolbar controls flat with a token ring ≥ 3:1;
   neumorphic depth only on stand-alone hero surfaces.

## 3. Global design-system changes

### 3.1 Layout tiers (`base.html`, `global.css`)
`{% block content_width %}` renders `data-width` on `<main>` (after `id`, which
stays the first attribute):

| value | `.content-wrap` | used by |
|---|---|---|
| `default` | 1120 px (unchanged) | reading pages |
| `wide` | 1480 px | available; the exam pages keep their own identical rule for now (separate PR) |
| `full` | none | tool pages — the workspace first |

`data-fill="viewport"` makes `.content-wrap` a full-height flex column whose
`.app-fill` region takes the remaining height; the footer becomes one 24 px
line (the attribution stays). Below 640 px tall the page scrolls instead and the
panes keep a 480 px minimum. On `full` pages the entry animation is off, so no
transform is left behind for fixed overlays. The `.page-header` bleed is made
padding-aware for `p-0`.

### 3.2 Tokens
- `--t4`: light `#66667f` (5.57 card / 4.92 bg), dark `#8c92aa` (5.72 / 6.19).
- `--text-brand`: light `#067858`, dark `#2ec9a0`.
- Primary: `--btn-primary-bg/-ink/-hover`: light `#067858` / white (5.48:1),
  hover `#05664b`; dark `#2ec9a0` / `#04201a` (8.13:1), hover `#4dd9b4`.
  `.btn-primary` and `.btn-success` use them; `.btn-outline-primary` text uses
  `--text-brand` with a real ≥ 3:1 edge.
- Severity: `--sev-critical|warning|info|ok` (+ `-bg`): light `#b42318`,
  `#b54708`, `#3b4fd6`, `#067858`; dark `#f97066`, `#f5b731`, `#8093ff`,
  `#2ec9a0` — each ≥ 4.5:1 as text on card and bg.
- Type: one scale, `--text-*`, with px floors: `--text-2xs` `max(11px,.6875rem)`,
  `--text-xs` `max(12px,.75rem)`, `--text-sm` `.8125rem`; `.fs-xs/.fs-sm/.fs-md`
  become 12 / 12.5 / 13 px; the fictional `--fs-*` comment block is replaced by
  the real scale; `.tnum` for tabular figures.

### 3.3 Components (new; `ux-` prefix like `ux-toast`)
- `.ux-toolbar`, `.ux-toolbar-group`, `.ux-sep`, `.ux-spacer`.
- Buttons: `.btn-icon` (32 × 32), `.btn-split` (joined; divider drawn with
  `::before`; the focused half rises above its sibling; logical radii).
- `.ux-menu` / `.ux-menu-item` (+ `-title`, `-desc`, `-sep`) and
  `static/js/ux-controls.js` (loaded by `base.html`, acting only on opted-in
  markup): a menu controller generalised from the workspace's menu script —
  open/close, outside click, Esc, arrows, Home/End, type-ahead, placement on
  the preferred edge, phone sheet — and the roving tabindex for `.ux-tabs`.
  Inline `style="display:none"` remains a menu's open/closed state. It
  replaces `static/js/timetable-workspace-accessibility.js`.
- `.ux-seg` / `.ux-seg-item` (+ `-count`) — segmented buttons with
  `aria-current`, not a tablist.
- `.ux-tabs` / `.ux-tab` (+ `.ux-tab-count`).
- `.ux-stat-strip` / `.ux-stat-group` / `.ux-stat` (`data-tone`), one `<dl
  aria-labelledby>` per scope.
- `.ux-meta` (· separated, empty parts vanish), `.ux-legend`.
- `.ux-pane`, `.ux-pane-head`, `.ux-rail` — titled, independently scrolling,
  collapsible panes whose rail shows a count.
- `.ux-dialog` — native `<dialog>` form surface.
- `.ux-job` — the shared planner job card, floating at the window's end edge.
- `.pill-n` — neutral status pill.

### 3.4 Global defects fixed on the way
- `.status-running` / `.status-failed` scoped to `.status-dot`.
- `html [hidden]{display:none!important}` — outranks utility `display`
  `!important` rules, including Bootstrap's `.d-flex`.
- The workspace's page-only rules leave `global.css` for
  `timetable-workspace.css` (loaded first in that file so its later rules keep
  winning); the shared `.dba-*` / `.sp-adv-wrap` dark rule stays global and is
  split onto its own line.

## 4. The workspace

```
┌ header  Timetable Workspace │ [Year][Sem] [Scenario ▾ ……][Draft] [New ▾][⋯]      [Optimise ▾][Export ▾][Publish · 4 blockers] ┐
├ boards  [Term 2  ⊘1 · 3 left][Term 4][Term 6]…[New board] │ [Filter / programme… 12/44] [↶][↷]        [Open ▾] [◧][◨]         ┤
├ stats   WHOLE SCENARIO: At-risk · Required · Flexible unseated · Students with a clash · Meetings without a room ║ INSTRUCTORS (n counted): With a teacher · Days · Idle ┤
├ ◧ Required sections │ Grid — per-table sticky headers, group jump list  │ Inspector: Issues · Selection · Capacity · Demand · MRI ┤
└ footer (one line) ─────────────────────────────────────────── floating job card (only while a job runs / just ended) ┘
```

**Header (scenario scope and scenario actions).** `#twGenBar` is the scope:
Year and Sem (they scope the scenario list only). Then the scenario picker
(capped, ellipsised, full name in its title), its status pill, **New ▾**
(Generate timetable…, Blank scenario…) and **⋯** (Copy of this scenario…, Edit
time slots…, Elective mapping…). At the end: Optimise ▾ and Export ▾ (neutral
split buttons — they act on the whole scenario) and Publish.

**Generate** opens a dialog with its own Year and Sem (defaulting to the scope),
programme chips from the known programmes, M / F / Both (Both disabled for the
new engine) and the engine choice; Generate is primary only there and in the
empty state. Portfolio (a one-option select the server ignores) is removed.

**Publish.** Readiness is fetched on scenario load and after each edit —
debounced, one request per data version (callers share it), quiet when it
fails — never on board switches. Board buttons and the Issues count include the
server's blockers on that board (a room that is missing or double-booked blocks
publishing, so it reads as critical, not as a warning). Not ready: a neutral "Publish · N
blockers" opening the blocker list (bilingual, structured, each linked to its
board). Ready: the filled primary; it confirms, lists warnings, and says the
scenario becomes read-only. A refused publish shows the server's blockers.

**Board bar.** Board buttons (segmented, not tabs) show the label, blocking
issues with the critical icon, and sections left to place; student counts go
to the tooltip and accessible name; overflow scrolls sideways. Then
the filter (also the programme lens — its placeholder says so), Undo/Redo,
**Open ▾** (Full MRI, Graph, Split view — real links that keep scenario and
board) and the two pane toggles.

**Stat strip.** Two scopes, each a labelled `<dl>`, on one line at 1366 and
wider:
- *Whole scenario*: At-risk students unseated (students with a failed course
  on record, or retaking one, left without a seat in a major or foundation
  course) · Required requests unseated (T1 + T2 over tolerance) · Flexible
  requests unseated (gen-ed, electives, foundations within tolerance) ·
  Students with a clash (unique students, not pairs) · Meetings without a
  suitable room. With tiering off: one "Unseated".
- *Instructors (n counted)*: Sections with a teacher (one-teacher rule on) ·
  Teaching days · Idle minutes. With no instructor data: "—" and the reason.

Board-level counts are not repeated in the strip (principle 6): blocking issues
are on the board's button and the Issues tab's count, sections to place on the
list's count and its rail, publish blockers on the Publish button. (A first
version had a third "Board" group; at 1440 it wrapped the strip onto three
rows and only repeated those.)

Values are neutral; tone is a start-edge bar plus an icon; the full meaning,
scope and direction are in each stat's tooltip. The scenario quality is cached
per scenario and edit — board switches no longer recompute it.

**Panes.** Left (Required sections) 248 px, collapsing to a 44 px rail with the
count of sections left; collapsed by default below 1600 px. Inspector 296 px
(320 px ≥ 1440, 352 px ≥ 1600). Below 1600 px the page title gives its row to
the controls (it stays the page's `<h1>` for assistive technology). Both remembered per user; `[` and `]` toggle them. At the
defaults a lecture slot is ≥ 96 px at 1366 and 1440. Columns are
`56px repeat(N, minmax(0,1fr))`; card lines ellipsise; the 650 px minimum goes.
Each table has its own sticky header; wrappers use `overflow: clip`.

**Inspector.** Issues · Selection · Capacity · Demand (the full course-demand
table, moved from the bottom panel) · MRI. Lock/Remove and every row use the
global neutral buttons.

**Job card.** Floats at the window's bottom end edge, so nothing in the page
moves. Shown while a job runs and after it ends, until closed; hidden when
idle. On this page it is a monitor (config `offerRebuild: false`): status,
fields, Cancel, View result. The card's legacy single-strategy rebuild ("Run in
background", and "Run again", which would repeat that rebuild whatever kind of
run had failed) is not offered — Optimise ▾ Full rebuild is the rebuild here.
Where a page does offer it, it asks first through the shared dialog. `#twGenStatus` is a live region that
takes no room while empty and is where focus lands when a menu choice starts a
job and disables its own trigger.

**Stage 1 coherence.** Cards get theme-aware ink and a dark tint; groups get
their real section label in natural order.

Stage 2: see §7 — the plan as revised after its audit and critiques, and the
decisions it rests on.

Stage 3 (to evaluate): a time-proportional week grid.

## 5. Contracts

- `<main id="main-content"` keeps `id` first.
- Removed elements are removed with the code that reads them, and their tests
  are updated in the same commit — no hidden stubs: `twStrategy`,
  `twBoardsNav`, `qsPlaced`, `qsConflicts`, `twBoardStrip`, the bottom panel
  (`twBottom*`), `twFullscreen`.
- Menus: `<button role="menuitem">` (or `<a role="menuitem">` for links) inside
  a `.ux-menu` with inline `style="display:none"`; `.ux-menu` never sets
  `display`. Tablists exist only where they behave as tabs.
- Template copy stays `{% if LANGUAGE_CODE == 'ar' %}…{% else %}…{% endif %}`.
- `.fb-search` capsules stay inside a `.fb-fit` row.
- Tests that pin a detail this design changes are updated in the same commit
  to pin the new contract; new tests are mutation-checked.

## 6. Acceptance

- 1366×650, 1440×790, 1536×730, 1920×1080; light and dark; English and
  Arabic; wide font: no horizontal page scroll; lecture slot ≥ 96 px at the
  default pane state; chrome above the panes ≤ 170 px at ≥ 1440; nothing in
  the frame below 11 px; text ≥ 4.5:1; control edges ≥ 3:1.
- Arabic: no letter-spacing on Arabic labels; the grid starts at the right;
  mixed number/label strings isolated with `<bdi>`.
- Publish with blockers lists them and never calls publish; ready publish
  confirms first.
- Full test suite green.

Pinned by `tests/test_timetable_workspace_frame_browser.py` (frame, slot width,
chrome height, type floor, contrast, blocked Publish never posts),
`tests/test_design_system_tokens.py` (token contrast in both themes),
`tests/test_timetable_toolbar_menus_browser.py` (every menu, eight widths, both
languages, phone rotation) and the jsdom suites under `tests/frontend/`.

## 7. Stage 2 — plan (2026-10-22, after a read-only audit and two adversarial critiques)

A seven-agent read-only workflow mapped the stage-1 code (cards, inspector,
keyboard, lens and slot edits, the design system), drafted a stage-2 spec, and
had it attacked by a design critic and an engineering critic. Their strongest
findings changed the plan:

- Place… as drafted (one day and time) cannot create a section of most
  courses: `create_planned_section_placements` requires the whole meeting
  pattern (SVC:2018-2021), so drag-to-place already fails for them.
- Re-running the plan lens after every edit is a second full seating
  evaluation per edit on the 0.5 vCPU / 512 MB host.
- The split view's slot editor is a second writer with the same data loss.
- The drafted card head does not fit a 96 px slot (about 70 px of content).
- The slot data defect was scheduled last.

### 7.1 Decisions (the owner delegated them; each can be revisited)

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Sections stacked in one cell with no server clash | **Not an issue**: no ring, no row | Red without a reason was the defect; parallel sections for other cohorts share times all the time |
| 2 | Empty room (`''`) | **Warning**, "Room not assigned yet" | Quality and the stat strip already count it; a neutral card would disagree with them |
| 3 | A room double-booked with another board | **Warning** | Not a publish blocker today; the page must not invent one |
| 4 | Meetings outside the grid's columns | **Warning**, and drawn in a parking strip under their table | Blocking would make scenarios unpublishable at once (online meetings saved through the old editors) |
| 5 | Filter and programme lens | The **text filter** collapses tables without a match and dims inside the rest; the **lens** dims | Finding things needs fewer tables; a view never hides a clash |
| 6 | Course hue | **Keep the tint, make it deterministic** (from the course key, not visit order), plus a same-course echo on focus or selection | The 10-hue palette is a weak signal; the bug is the visit order |
| 7 | Severity on cards | Critical and warning ring; **info never rings**; counts are critical (or warning when none), never info | Info rings and info counts diluted severity |
| 8 | A room double-booked on the same board; a meeting that needs a room and has none (`UNASSIGNED`) | **Critical** | Both block publishing (`board_room_conflicts`, `board_sections_without_rooms`) |
| 9 | A cross-board student overlap | **Warning** at the publish check's line (`hard_overlap_threshold`, sent with the conflicts), **info** below it | The publish check warns at that line; the page uses the server's number, not a copy |

### 7.2 Pull requests, in order

1. **Slot columns never lose data** (first, unconditionally).
   - Server: every column validated; flags carried from the column a row
     replaces (matched by start and end); a change that would leave drawn
     meetings off the grid, on any board, is refused with their list until
     the request names their fingerprint; the pages get the effective columns.
   - Both editors (workspace and split) use one shared module
     (`timetable-slot-config.js`), list the effective columns and confirm
     hidden meetings by name. Neither page keeps defaults of its own.
   - A manual move is never offered a policy column, nor an evening
     (online-only) one unless the meeting is already in one.
   - The per-plan export lists an off-grid lecture under its grid instead of
     failing.
2. **One issue model** (correctness before appearance).
   - Server: per placement `is_online`, `needs_room`, `room_state`,
     `shared_with_other_scenario`; meetings prefetched (one query per board).
   - Client: one memoised model whose inputs carry fresh / stale / unknown;
     severity follows the server per §7.1; no "UNASSIGNED" anywhere; card
     tones updated in place (never a grid rebuild mid-drag); the MRI tab reads
     the same counts; other boards' buttons use fresh readiness items.
3. **Card anatomy and legend.** One icon per line, a room token that never
   shrinks, the teacher only where the card is wide enough (container query),
   selected = tint + `aria-current`, focus = the global ring, highlight = an
   animation; a legend after the last table; browser checks that a
   seven-character code is never truncated at 96 px in the wide font.
   As built: selected is a neutral ring (`--t0`) with `aria-current`, the
   course's other meetings a dashed echo; the lock and shared marks are global
   `.ux-icon`s and the legend a global `.ux-legend`; "No room" is bold in the
   card's own text colour (red text cannot reach 4.5:1 on every course colour
   — the ring and the octagon carry the severity); the course colour comes
   from the course key by hash, not from drawing order.
4. **Inspector.** Global `.ux-disclosure` groups (critical / warning / info,
   info collapsed), rows as buttons, "This section's issues" at the top of
   Selection, teacher states, 0 versus unknown, Seated on demand (a Refresh,
   never a per-edit lens reload).
5. **Keyboard.** Grid semantics for the board (days × columns) with roving
   focus; the left list as one roving stop; `event.code` for `[` and `]`; a
   Move dialog with one ranked list whose verdict follows the server's
   evidence kinds; Place… as a pattern picker over `meetings[]`; Change room
   from `/room-candidates/` (allowed on time-locked meetings).
6. **What the grid shows.** The lens from one server function shared with the
   per-plan export (roles including out-of-plan), its cost measured first and
   one evaluation per data fingerprint shared with quality; filter and lens
   per §7.1; the parking strip; the slot dialog replaces the textarea on the
   server contract from PR 1.

Follow-up (recorded in PR 2's review): the stat strip's "Meetings without a
suitable room" is toned critical whenever the quality check reports any
`missing` or `clash`, but that count also holds blank rooms and cross-board
clashes, which §7.1 rates a warning. Telling them apart needs the server to
split `missing` into unassigned and blank (and same- from cross-board
clashes); until then the strip can read more severe than the cards.

Every PR: the frame acceptance of §6, its own version bumps, mutation-checked
tests, and Arabic plurals through `Intl.PluralRules`; every time range in
Arabic text isolated.
