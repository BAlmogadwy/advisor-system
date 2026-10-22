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
- `.ux-disclosure-toggle` (+ `-label`, which takes the free width, and
  `-chevron`): a button that opens and closes the region it controls, from one
  delegated listener in `ux-controls.js`; with `data-ux-disclosure="key"` the
  choice is kept for the session (in memory where storage is unavailable).
  The chevron points down when open and along the reading direction when
  closed. (Section planning's `.sp-chev` predates it and is a candidate to
  move onto it.)
- `UXControls.roving(region, options)` (and `UXControls.letterOf(event)`):
  the WAI-ARIA grid pattern for a region of `[role=grid] > [role=row] >
  [role=gridcell]` — one tab stop, arrows (mirrored in RTL; ↑/↓ also walk the
  stops stacked in one cell), Home/End, Ctrl+Home/End, PageUp/PageDown between
  tables, Enter/Space to activate, a map of letter keys (by `letterOf`: the
  Latin letter typed, or the key's letter where it types another script's).
  It keeps the active stop (by `data-focus-key`) through any redraw and gives
  the keyboard back when it was in the region; a restore scrolls only after a
  key. `.ux-grid-row` (display: contents) is a row on a CSS grid; a roving
  cell takes an inset focus ring; `.ux-keys` is the keys line shown while the
  keyboard is in a region. `--ux-job-block` is the floating job card's height,
  for scroll padding.
- `UXControls.redraw(region, html)`: draws a region again without losing the
  keyboard — unchanged content keeps its nodes; changed content gives focus
  back to the same control (`data-focus-key`, `data-ux-disclosure` or id), or
  to the region while that control is gone or disabled, and returns it once
  the control is enabled again unless the user moved on. A control switched
  off in place while it has the keyboard (a write in flight disables the
  page's mutation controls; Chromium then lets go of the focused one) is
  held the same way: the region takes the keyboard and the control gets it
  back when it is switched on again, in place or by a redraw. A page that
  redraws a panel after every server answer uses it instead of `innerHTML`.
  The focused field keeps an edit not yet committed when the page draws the
  same field (key and data attributes) from the same value, and only if the
  field can hold it (a new value from the page wins; a multiple select is
  drawn as the page says). Limits: not a caret
  or text selection; the region needs a `tabindex` to hold the keyboard.
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
   `.ux-icon`s, the tone mark an announced image, and the legend a global
   `.ux-legend` (its workspace swatches stay page-local); the room word never
   shrinks — the teacher gives way and the marks go to their own line when
   needed (three marks rarely fit beside a room in ~70px); "No room" is bold
   in the card's own text colour (red text cannot reach 4.5:1 on every course
   colour — the ring and the octagon carry the severity); the warning ring and
   mark use a darker amber on light cards (3:1 on every course colour); the
   course colour comes from the course key by hash, not from drawing order —
   a guide to finding a course, not its identity (20 colours can repeat).
4. **Inspector.** Global `.ux-disclosure` groups (critical / warning / info,
   info collapsed), rows as buttons, "This section's issues" at the top of
   Selection, teacher states, 0 versus unknown, Seated on demand (a Refresh,
   never a per-edit lens reload).
   Built in two parts. **4a**: a global disclosure (`.ux-disclosure-toggle`,
   one delegated listener in ux-controls.js, the choice kept per session and
   key) for the Issues groups (context starts closed; counts stay visible);
   Selection lists the section's own issues (critical and warning, each with
   its subject), its teacher by the one-teacher rule — now one shared module,
   `timetable-section-teacher.js`, for the workspace and split view (they
   disagreed) — and a count of 0 as 0; the meetings list names no teacher (a
   meeting row can carry a stale name), and neither does search. The panel is
   drawn through the global `UXControls.redraw`, so the several redraws one
   edit causes neither move the keyboard nor disturb a screen reader; an item
   from before the last edit says so in Selection as in Issues. **4b**: the programme mark (#160
   parity): its words and parts move from split view into a shared module,
   `timetable-programme-mark.js`, which both pages wrap in their own markup;
   Selection shows the mark, the "nobody needs it" warning, the seated mix
   and the grouping trade-off. The seated mix comes from the lens the page
   holds: after an edit it says it is from before the change and offers a
   Refresh (the lens is a full seating evaluation, never re-read per edit); a
   saved mark re-reads it once.
   As built after review: a saved mark reads the lens, then the quality and
   the publish check (seats and student clashes are seating), one at a time
   on the small host; a mark saved again unchanged moves no version
   (`twFetch`'s `_changed`), so nothing turns stale; one lens read per data
   version, the newest answer winning; a section the lens does not hold reads
   "—", not "nobody". The select is under the page's edit guard
   (`data-tw-mutation`, with `data-tw-hold` keeping it off for its own
   reasons) and keeps the keyboard through a save (the global redraw). A
   choice made from the keyboard saves once the arrow keys rest, or on Enter
   or on leaving — Chrome on Windows fires `change` per arrow key — in both
   pages (`TimetableProgrammeMark.saveOnSettle`, which reads the section and
   the value when the choice is made and keeps one wait per section: a
   repaint before the keys rest loses nothing and saves nothing in between,
   and a select a redraw takes off the page — Chromium blurs it as it goes —
   is not "left"). A visually hidden status line says when the mix has been read
   again. While a mark saves, the select shows the mark being saved; a
   keyboard choice the page turned busy for is not dropped silently (the
   saved mark comes back and a warning says why); a reset lens (the scenario
   reloaded after a planner job) forgets the read in flight. The global
   redraw keeps an edit the user has not committed in the focused field
   when the page draws that field from the same value.
5. **Keyboard.** Grid semantics for the board (days × columns) with roving
   focus; the left list as one roving stop; `event.code` for `[` and `]`; a
   Move dialog with one ranked list whose verdict follows the server's
   evidence kinds; Place… as a pattern picker over `meetings[]`; Change room
   from `/room-candidates/` (allowed on time-locked meetings).
   Specified in full in §7.3 (six sub-PRs, 5a–5f). **As built, 5a (keyboard
   foundation):** the board is an ARIA grid per table (rows `.ux-grid-row`,
   day row headers, "Slot N, time" column headers, cells keyed
   `cell:<group>:<kind>|<day>|<start>`, cards `role=button` keyed
   `placement:<id>`, a stack's position said); the list is one one-column grid
   per term (rows keyed `place:<course>|<label>` or `ts:<id>`, a complete
   course reachable and `aria-disabled`, the English button for the pointer
   only); both are one tab stop through the global `UXControls.roving`. Enter
   selects a card (said in `#twBoardStatus`, outside both panes), Escape
   clears the selection (else passes), Enter on a list row opens an English
   row's pattern or says what to do; an Issues locate moves the board's tab
   stop, never the keyboard, and says where; the pane keys work by key
   position (ج/د on an Arabic layout) and collapsing the pane with the
   keyboard moves it to its rail; busy is `data-busy` (a progress cursor),
   never `aria-busy` on the workspace. Found on the way: Chrome's
   `checkVisibility()` is false for a `display: contents` row, so rows are
   checked for `hidden`/`.d-none` only; the visually hidden status line,
   absolutely positioned, stretched the page until `.tw-centre` became its
   containing block; dark mode's hover hid the severity ring (now a
   `--tw-ring` variable kept by hover and focus). Browser tests share a helper
   (`tests/timetable_workspace_browser_support.py`). After review: a region
   that gains a box (the list, collapsed at load below 1600px) is read again,
   so it has its one stop when it opens; the keys lines sit outside their
   scrollers (the board's scroller is now `#twGridWrap` inside a column
   `.tw-centre`), so they never cover a stop or scroll away sideways, and
   the stop is kept in view as a line appears; a selected card with the
   keyboard shows its selection ring inside the focus ring; the pane keys
   read the key typed first and fall back to the key's position only for
   another script's letter (ج on Arabic, never German ü), and opening a pane
   from its rail keeps the keyboard; Enter on an English row the page cannot
   act on says why, and the keyboard returns to the list after the chooser;
   PageUp/PageDown in the list go to a term's first row; a pending key waits
   for two redraws at most; a key another handler took, Ctrl+arrows and an
   IME composition are left alone; each card names its own day and time (a
   screen reader may not say a cell's headers while the focus is on a card).
   Not yet done: the manual NVDA/JAWS check of D6c (no screen reader on the
   build machine; the accessibility-tree test guards Chrome's exposure).
   **As built, 5b (placing a planned section from the list):** the server
   (its own PR) names the labels placed anywhere in the scenario
   (`used_section_labels`, natural order) and replaces a reused
   workspace-made section's meeting rows (`tw_planned`, `tw_auto`,
   `tw_scheduler`; scenario-owned; placed on no board of any scenario) —
   without the spec's blank-instructor guard, since one-teacher mode writes
   the engine's names into those rows and they are derived. The page
   proposes the first free labels (S2 with S1 and S3 placed; an older server:
   the first `used_sections` count as used), a drop names the course (key and
   name), and a drop of a course that meets N times a week is refused before
   any request — and the server's own "complete pattern" refusal reads the
   same, re-reading the budget (with no count invented when the message has
   none) — with the count (`pluralAr` in Arabic) and, until Place… (5f), a
   pointer to split view, which places a whole pattern; Enter on such a row
   says the same.
   **As built, 5c-audit (server, its own PR):** room candidates answer, on
   `?audit=1`, what the quality audit will say once each room is saved; the
   audit's room rule moved into one module both use (§7.3, 5c-audit). No
   client change yet.
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

### 7.3 PR 5 — keyboard, Move, Place, Change room (spec)

This spec was written from four drafts (grid, move, place, room) and two critiques (UX and engineering). Every claim where the drafts and the critics disagreed was checked against the code in `C:\Users\user\myUniproject-wt\programme-mark` at commit `7116f53` (the tip of the 4b branch). Line numbers below are for that commit.

**Abbreviations**
- PAGE = `static/js/page-timetable-workspace.js`
- UXC = `static/js/ux-controls.js`
- GCSS = `static/css/global.css`
- TWCSS = `static/css/timetable-workspace.css`
- TPL = `core/templates/core/timetable_workspace.html`
- VIEWS = `core/timetable_workspace_views.py`
- SVC = `core/services/timetable_workspace.py`
- POLICY = `static/js/timetable-meeting-policy.js`
- PANES = `static/js/timetable-workspace-panes.js`
- SPLIT = `static/js/page-timetable-workspace-split.js`

**Notation**
- `{t}` is a time range, always isolated. In HTML it is written `<bdi dir="ltr">09:00–10:15</bdi>`. In plain text (an `aria-label`, a toast, a status line) it is wrapped in the escapes `\u2066…\u2069`, never the literal characters. Check every diff with a byte search for `[\u2066\u2069\u200e]`.
- `{c}` is a course code or section. In HTML it goes in `<bdi>`; in plain text it gets the same escapes.

#### 7.3.0 Decisions

| # | Decision | Evidence (code) | Resolves |
|---|---|---|---|
| D1 | PR 5 ships as **six sub-PRs (5a–5f)**. Each one can ship alone and carries its own tests. | The drafts held four server changes, about 60 tests and four Playwright files: too much for one reviewer. | PR size (eng) |
| D2 | **One dialog layer for Move, Place and Change room**, listed below. The drafted `UXControls.rankedChoice`, the dialog built in JS, and every `.ux-rank-*` class are dropped. | The existing `.ux-choice` already has a hidden radio, `:has(input:checked)` and `:focus-within` (GCSS:2355-2373). jsdom has no `showModal`; Generate falls back to the `open` attribute (PAGE:587-588). | Blocking findings from both critics |
| D2a | The layer is `UXControls.dialog` (open, close, isOpen). It acts only on dialogs it opened. | Generate wires its own close (PAGE:596-599); the English chooser has its own `cancel` handler (POLICY:90). | |
| D2b | `.ux-dialog-scroll` shell. | | |
| D2c | Rows in `.ux-choice-list` are a native radio inside `label.ux-choice[data-tone]`. A row that cannot be chosen is `.ux-choice-static` with its reason as visible text. **There are no disabled radios.** | | |
| D2d | One delegated keyboard for `[data-ux-submit]` lists. | | |
| D2e | Each dialog is a template `<dialog>` with its Arabic copy in `{% if %}` arms (§5 contract). | | |
| D3 | **One gate, `cardActionRefusal(kind, p)`, with one table of refusal words**, used by the keys, the Selection buttons and the dialogs. | `workspaceBusy()` stays true until quality returns after every write: `boardLoading` is cleared only in `finally` (PAGE:1019-1061). A gate that refused while busy would refuse the second M for seconds. | Gate findings (both critics) |
| D3a | It refuses only for: published; board not yet loaded; a planner job in `ACTIVE_PLANNER_STATUSES` (PAGE:82); another scenario's section; a time lock (Move only); online or no room needed (Change room only). | | |
| D3b | A board refresh or a write in flight never refuses opening a read-only preview. Only the commit is held, through `[data-tw-mutation]` plus `[data-tw-hold]` (PAGE:106). | | |
| D3c | The gate runs **before** any English or policy routing. | `chooseEnglishWorkspacePattern` returns silently when the page cannot edit (PAGE:2210). | |
| D4 | **One seam: `TW.keyActions.{move, room, place}(subject, {origin})`.** `origin` is a focus key, never a DOM node. | | Card-key binding findings |
| D4a | M and R are bound only in the grid controller's `keys` map. No other listener reads M or R. | | |
| D4b | No `aria-keyshortcuts` anywhere. | Neither key activates the element it would sit on. | |
| D5 | **One owner of focus after a write: the roving controller.** | jsdom 29 sets `activeElement` to body when the focused node is removed, then runs the MutationObserver (eng probe). | Post-commit focus (both critics) |
| D5a | Dialogs resolve `returnFocus` from the origin key at close time. | | |
| D5b | On success: close the dialog, then `nav.active(key)` without focus, then `await selectBoard`. Rule R5 restores focus only when the keyboard was in the region and is now nowhere. | | |
| D5c | There is no `TW.pendingFocusPlacementId`, no re-focus after `selectBoard`, and no focus step after Place. | | |
| D6 | **Board focus goes to cards** (`role=button`) inside gridcells. | Cards stack vertically (TWCSS:225-233). | Grid D1–D3; NVDA risk (UX) |
| D6a | An empty cell is its own stop. ↓ and ↑ walk the cards stacked in one cell. | | |
| D6b | There is one tab stop for the board and one for the list. | | |
| D6c | Before 5a merges, a **manual NVDA + Chrome check** (JAWS if available) in English and Arabic. If browse mode keeps the arrows, use the fallback in 5a §Risks. | | |
| D7 | **The left list is a one-column layout grid per term**: `role=grid` > `row` > `gridcell`. The gridcell is the stop. | ARIA 1.2 allows `aria-haspopup` on `gridcell`, not on `option`. | List-semantics findings (both critics) |
| D7a | No listbox, no `aria-selected`, no P key. Enter and Space activate the row. | | |
| D7b | Keys: `place:<course_key>\|<label>` for budget rows, `ts:<id>` for unplaced rows. | | |
| D7c | The same controller drives the board and the list. | | |
| D8 | `letterOf(event)`: if `event.key` is a Latin letter, use it. Otherwise use `event.code` **only when `event.key` is a single non-Latin letter** (`/^\p{L}$/u`). Punctuation and digits give `''`. | AZERTY's KeyM types `,`; Dvorak's KeyR types `p`. The grid draft's formula fired on `,` (its own R5 test forbade that). | Letter-keys findings |
| D9 | A restore always focuses with `preventScroll:true`. It calls `scrollIntoView` only when the last input was a key. Pointer and wheel input clear that flag. | A planner job ending calls `onScenarioChange` → `selectBoard` → `renderGrid` with no key pressed (PAGE:162-166). | Passive-scroll findings |
| D10 | **Keyboard announcements go to `#twBoardStatus`**, in `#twCentre`, outside both panes. | `.tw-right > :not(.tw-rail)` is `display:none` when the inspector is collapsed (TWCSS:121-122), and `#twInspectorStatus` is inside it (TPL:265). | aria-busy and announcer finding (UX) |
| D10a | `.tw-workspace` loses `aria-busy`. The progress cursor moves to `data-busy`. | PAGE:108; TWCSS:343-344. | |
| D11 | **An Issues locate never moves focus.** It moves the board's tab stop to the first card and announces where it is. | Locating on another board redraws the list (PAGE:2545-2556). | Locate finding (UX) |
| D12 | **A time lock refuses Move. The dialog never offers an override.** | Owner rule: a lock freezes days and times, never the room. Matches drag (PAGE:2041-2042), split (SPLIT:7109-7114) and the pattern switch (PAGE:2219). | Move "Open" #1 (decided by owner rule) |
| D12a | Change room is allowed on a locked meeting. Another scenario's section refuses both. | VIEWS:3017-3025. | |
| D13 | **A server verdict for both slot previews, behind `include_verdict`** (query `verdict=1`), using the publish check's rules. | | Verdict findings (both critics), H15 side effects, own-section slots, blocked times, cost to other callers |
| D13a | Rules: H15 for same-board students, including **its effect on untouched pairs**; blocked times are flagged, not removed; a same-board room clash is critical; cross-board students are a warning at or above the threshold and info below; a cross-board instructor is a warning (the publish check does not block it, so the page must not invent a blocker). | SVC:4012-4210; H15 SVC:585-601. | |
| D13b | The section's own other meeting slots are flagged `own_section_meeting` and cannot be chosen. | `SectionPlacement` is unique on (board, term_section, day, start_time) (core/models.py:1040-1041), and the move answers 409 on a duplicate (VIEWS:3064-3069). | |
| D13c | A new `verdict_rank`. Legacy fields and order are unchanged. | | |
| D13d | Other callers (safe moves, repair, move outcome) do not pay for it. | Callers: SVC:3115, 3215; timetable_repair.py:6995; timetable_move_outcome.py:53. | |
| D14 | A manual move is offered evening (online-only) columns only when the meeting is already in one (`offer=manual`). | §7.2 PR 1. JS rule at timetable-slot-config.js:166-169. | Move "Open" #3 (decided by the design record) |
| D15 | **Place sends one GET per open** (`limit` at least the total, `verdict=1`). The Day/Time filter runs on the client. | Planned `score_slot` runs once per meeting per pattern, with no memo (SVC:1707-1715). The preview lane has two tokens (request_lanes.py:33). | Place load finding |
| D15a | The planned `score_slot` is memoised per cell. No `includes_*` parameters, and `saveOnSettle` is not moved. | | |
| D16 | **English sections use the existing chooser on every path in PR 5.** | Split view also uses `TimetableMeetingPolicy.choose`. | English findings (both critics) |
| D16a | The shared gate runs first; "Finding patterns…" is announced before the fetch; focus follows the recreated rows. | | |
| D16b | Accepted gaps, each a recorded follow-up: the chooser's counts without `pluralAr`, disabled reasons only in a `title`, focus returned to a stored node (POLICY:50, 69, 81). The page calls `boardNav.restore()` after the chooser resolves, which covers the stored node. | | |
| D17 | **While a dialog is open, every message is shown inside it, never as a toast.** Toasts go into a body-level container (notify.js:6-13) that is inert behind a modal. | | Inline-errors findings |
| D17a | **Escape during a commit closes the dialog.** The write finishes and its result arrives as a toast after the close. | | |
| D17b | `twFetch` fixes: announce the planner job of `SCENARIO_BUSY` before `_onError` and `_quiet`; call `_onError(null, status)` when the body is not JSON; call `_onError(null, 0)` when fetch rejects. | Today `_onError` runs first (PAGE:204-208), a non-JSON body skips it (PAGE:212), and a rejection skips it (PAGE:219-221). | |
| D18 | Before a commit button turns off, focus moves to the checked radio. | | Commit-focus findings (both critics) |
| D18a | `aria-busy` goes only on `.ux-choice-list`. Status lines sit outside it. | | |
| D19 | **Preview reads keep one promise in flight per key**, joined again on reopen and never aborted. | The lane token is released only when the view returns (request_lanes.py:63-80). Precedent: `loadPlanLens` (PAGE:1655-1685). | Preview-lane finding (eng) |
| D19a | The memo key includes `TW.dataVersion`. Retry skips the memo but still joins a request in flight. | | |
| D20 | **Change room** reads the room list at the server's current time (no slot parameters; VIEWS:2821-2823, SVC:2152-2154). | | Room findings |
| D20a | Its commit body is `{room}` only. That keeps the change room-only and keeps the lock (VIEWS:2997-3013). | | |
| D20b | A room that fits (type, gender, seats) can be chosen even when another section holds it at that time. This follows the owner ruling of 2026-10-20 (SPLIT:4254-4269). | | |
| D20c | Rooms that do not fit are listed with their reason and cannot be chosen, as in split view (SPLIT:7095-7098). Owner question Q1. Amended by 5c-audit (judged by the quality audit; seats a choosable warning); the client PR applies it. | | |
| D20d | The server stops counting the same class's copies as occupants and always returns the current room. | | |
| D21 | `timetable-room-choice.js` is a shared module whose only user in PR 5 is the classic page. A parity test checks it against split's functions. Split switches to it in a follow-up. | | Split-refactor scope (eng) |
| D22 | Fixes to the drag path in 5b: | | Place data defects |
| D22a | The `create_planned` drop sends `course_key` and `course_name`. | Today it sends neither (PAGE:2301-2315), and the view defaults the key to the code (VIEWS:1876). | |
| D22b | Section labels come from `used_section_labels`. | Today they are `S${used+1}` (PAGE:1173). | |
| D22c | A drop of a multi-meeting course is refused in 5b and opens Place… in 5f. | | |
| D22d | Old meeting rows of a reused planned section are replaced. | `get_or_create` at SVC:2033-2068. | |
| D23 | Undo of a section create removes every meeting in reverse order. On partial failure the remaining ids stay on the undo stack and the board is reloaded. | `runHistoryAction` returns before `selectBoard` on null (PAGE:2416-2433). | Partial-undo finding |
| D24 | After a write, the report reads the board's issue model. Drag and the dialogs share it for move and place. | It replaces `validate_placement`'s count (PAGE:2369-2373), which can disagree with the card's ring. | Move "Open" #4 |
| D25 | The keys are shown in a line that is visible **while the keyboard is in its region** (global `.ux-keys` plus the controller's `hint` option). They are not in the legend. | The legend renders only when there are placements (PAGE:1947). | Key-discovery and column-label findings |
| D25a | `#twGridKeys` describes only the board's first table. Column headers read "Slot N", not "Column N". | | |
| D26 | Escape on the board clears the selection when one exists (split parity, SPLIT:9443). Otherwise the key passes through. | | Grid "Open" #3 |
| D27 | Arabic register: | | Strings findings |
| D27a | Verbal nouns on buttons: نقل… · وضع… · تغيير… · تعيين القاعة · إعادة المحاولة. | | |
| D27b | عن بُعد for online; كليهما for "both" (PAGE:3220). | | |
| D27c | "Published" reuses PAGE:1392; "busy" reuses PAGE:130. | | |
| D28 | **Asset versions.** Each sub-PR bumps every asset it changes to (the highest `?v` on master or any open branch) + 1, and re-bumps after any merge that touches the same asset. | Memory: identical bumps on two branches merge silently. Today's tip: `global.css` 81, `ux-controls.js` 3, `timetable-workspace.css` 11, PAGE 49, PANES 1, POLICY 4, SPLIT 136. | Asset findings |
| D29 | While the floating job card (`.ux-job`, GCSS:2378-2386) is shown, the board keeps focused content clear of it with `--ux-job-block`. | | WCAG 2.4.11 finding |

#### 7.3.1 Sub-PRs

| PR | Contents | Depends on | Server |
|---|---|---|---|
| **5a** Keyboard foundation | `UXControls.roving` and `letterOf`; grid and list semantics; Enter selects; Escape clears; Issues locate; panes keys by `event.code`; `#twBoardStatus`; `aria-busy` fix; keys lines; scroll padding | — | none |
| **5b** Placing a planned section from the list | Drop sends identity; free labels; multi-meeting drop refused clearly; old meeting rows replaced | — | budget `used_section_labels`; `create_planned_section_placements` |
| **5c** Dialog layer + Change room + R | `UXControls.dialog`, `.ux-dialog-scroll`, `.ux-choice-list`, `.ux-keys` styles, `twFetch` fixes, gate and words, `TimetableRoomChoice`, `#twRoomDialog`, Selection's Room button | 5a | room candidates: `term_section_id`, same-class copies skipped, current room always returned |
| **5d** Slot verdict | One verdict builder for both previews behind `include_verdict`; `offer=manual`; memoised planned scoring | — | yes, Python only |
| **5e** Move + M | `#twMoveDialog`, Selection's Move…, `commitPlacementMove` shared with drag, the edit report, English routing fixes | 5a, 5c, 5d | none |
| **5f** Place… | `#twPlaceDialog`, list rows activate Place, drop opens Place filtered, `section_create` history | 5a, 5b, 5c, 5d | none |

**Order:** 5a and 5b in parallel, then 5c, then 5d, then 5e, then 5f.

**Every sub-PR:** the frame acceptance in §6; its own version bumps (D28); every new test mutation-checked (apply the mutant, watch the test go red, revert); an "As built" note under §7.2 item 5; and §3.3 and §5 updated for any new global component.

#### 7.3.2 Contracts shared by several sub-PRs

##### A. Gate (page-local, PAGE)

`cardActionRefusal(kind, p)` returns `''` or `{code, text}`. `kind` is `'move'` or `'room'`. The first condition that applies wins:

1. `TW.scenario?.status === 'published'` → `published`
2. `!TW.boardId || TW.loadedBoardId !== TW.boardId` → `loading`
3. `ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)` → `job`
4. `p.shared_with_other_scenario` (VIEWS:471) → `external`
5. `kind === 'move' && p.is_locked` → `locked`
6. `kind === 'room' && p.is_online` → `online`
7. `kind === 'room' && p.needs_room === false` (VIEWS:469) → `noRoom`

Codes 6 and 7 mean "not applicable": Selection shows no button for them. Codes 1–5 mean "held": the button is drawn disabled, with its reason.

`anyDialogOpen()` = `TW.englishPicker || document.querySelector('dialog[open], .dlg-backdrop')`. Every key action and every dialog opener returns early when it is true.

`runCardKey(kind, card)`:
- If `anyDialogOpen()`, return `false` (the key is not handled).
- If the gate refuses, call `notify.warning(text)`. No dialog is open at that point, so the toast is seen.
- Otherwise call `TW.keyActions[kind](p, {origin: 'placement:'+p.id})`.

**Refusal words.** One page dictionary, `REFUSAL`:

| code | English | Arabic |
|---|---|---|
| published | This scenario is published and read-only. (PAGE:1392) | هذا السيناريو منشور ولا يمكن تعديله. |
| loading | The board is still loading. | اللوحة ما زالت قيد التحميل. |
| job | A planner job is running; edits wait until it ends. | مهمة المخطِّط قيد التشغيل؛ يمكن التعديل بعد انتهائها. |
| busy (commit only) | Wait for the current operation to finish. (PAGE:130) | انتظر انتهاء العملية الحالية. |
| external | Another scenario owns this section's time and room; change it there. | يملك سيناريو آخر وقت هذه الشعبة وقاعتها؛ عدّلها هناك. |
| locked | This section's time is locked. Unlock its time to move it; its room can still change. | وقت هذه الشعبة مقفل. افتح قفل الوقت لنقلها؛ ويمكن تغيير قاعتها. |
| online | This section is online; it needs no room. | هذه الشعبة عن بُعد؛ لا تحتاج إلى قاعة. |
| noRoom | This section needs no room. | هذه الشعبة لا تحتاج إلى قاعة. |

##### B. Origins and focus

**Origin keys:**
- `placement:<id>` for a card.
- `sel:<kind>:<id>` for a Selection button.
- `place:<course_key>|<label>` or `ts:<id>` for a list row.

`resolveOrigin(origin)`:
- `placement:` → `boardNav.element(origin)`, else `boardNav.element()`.
- `place:` or `ts:` → `listNav.element(origin)`, else `listNav.element()`.
- `sel:` → `#twRightBody [data-focus-key="<kind>"][data-placement-id="<id>"]`, else `#twRightBody` (it has tabindex 0, TPL:262).

**After a write:**
1. Close the dialog.
2. `boardNav.active('placement:'+id)` (or `listNav.active(key)`).
3. `await selectBoard(boardAtStart)`.
4. `updateUndoRedoButtons()`.
5. Report (contract D).

##### C. Dialog commit (page-local helper `dialogWrite`)

```
dialogWrite(dialog, url, body) -> Promise<{data} | {code}>
```

1. Focus the checked radio (or the list) **before** the request. `twFetch` turns `[data-tw-mutation]` off synchronously (PAGE:177, 106).
2. Call `twFetch(url, {method:'POST', headers, body: JSON.stringify(body), _quiet: true, _onError: (b, s) => (code = b?.error?.code || (b ? String(s) : 'NETWORK'), true)})`.
3. If the answer is null, return `{code}`. Otherwise return `{data}`.

`dialogSay(dialog, text, tone)`:
- While `UXControls.dialog.isOpen(dialog)`: put the text in that dialog's `[role=alert]` line.
- Otherwise: `notify[tone](text)`.

**`twFetch` changes (PAGE:197-221)**, shipped in 5c:

```js
if (!response.ok) {
  let d = busyBody; if (!d) { try { d = await response.json(); } catch { d = null; } }
  const e = d?.error;
  if (e?.code === 'SCENARIO_BUSY' && e.details?.active_job) announcePlannerJob(e.details.active_job, e.details.scenario_id, e.details.active_job.job_id);
  if (typeof _onError === 'function' && _onError(d, response.status)) return null;
  if (_quiet) return null;
  /* message as today */ notify.error(msg); return null;
}
/* catch (err): */ if (typeof _onError === 'function' && _onError(null, 0)) return null; if (!_quiet) notify.error(…)
```

The two existing `_onError` users (PAGE:1461, 3464) already read `body?.`, so a null body is safe for them.

##### D. Report after an edit (5e; drag shares it)

`reportEdit(verbWords, ids)`:
- Runs after `selectBoard`.
- Reads `buildBoardIssues().rows` (PAGE:3355) for those ids, skipping rows marked `pending`.
- If any critical or warning row remains: `notify.warning(`${verb} ${TONE_LEAD[tone]}: ${labels}`)`.
- Otherwise: `notify.success(verb)`.
- Then `notifyInstructorClashBackstop(data)` as today (PAGE:2271-2279).

##### E. Preview reads (5c, used by 5e and 5f)

`previewRead(key, url)`:
- `PREVIEW` is a Map from `${key}|${TW.dataVersion}` to a promise and, once settled, its answer.
- A second call joins the promise in flight. Requests are never aborted.
- A token on the dialog decides whether a late answer is drawn.
- `selectBoard` drops **settled** entries when board data arrives (PAGE:1028). Entries still in flight stay joinable.
- Retry and "Check again" pass `{fresh: true}`: they skip the settled answer but still join one in flight.

---

#### 5a: keyboard foundation

##### Global: `UXControls.roving` and `UXControls.letterOf` (UXC; exported in the object at UXC:396; a `ROVING` paragraph added to the header comment)

```
UXControls.roving(region, options) -> nav      // once per region (WeakMap); a second call merges options
  stop      selector of stops inside a [role=gridcell]; a cell holding none is itself a stop
  initial   () => key | ''                    // the stop for a region seen for the first time
  activate  (stop, event) => void             // Enter and Space; default stop.click(); never on event.repeat
  keys      { M: fn, R: fn, Escape: fn }      // one Latin capital → matched by letterOf; any other name → event.key
                                              // fn(stop, event); returning false means not handled (no preventDefault)
  hint      Element: hidden = false while the keyboard is in the region (last input a key), hidden again on leaving
nav.active() -> key | nav.active(key, {focus = false}) -> key   // a key not yet drawn waits (pending) until a stop has it
nav.element(key = nav.active()) -> the connected stop or null
nav.refresh()   nav.restore()   nav.reset()
UXControls.letterOf(event) -> 'A'..'Z' | ''   // D8
```

**Structure, read on demand (no cache):**
- Tables are the `[role=grid]` elements in the region, in document order.
- Rows are the `[role=row]` elements that contain a `[role=gridcell]`.
- Cells are the `[role=gridcell]` elements of a row.
- A cell's stops match `stop`; a cell with none is its own stop.
- An element is skipped if it, or an ancestor up to the region, has `[hidden]` or `.d-none`, or if `el.checkVisibility?.() === false`. (jsdom has no `checkVisibility`.)
- **List-shaped** means every row of the region holds one cell.

**Keys.** They are handled only when the target is a stop, is inside a stop, or is the region itself. On the region, a navigation key focuses the active stop. Keys are never handled when `event.isComposing` is set or Alt, Meta or Shift is held. Ctrl counts only with Home and End. Every navigation key calls `preventDefault`.

| Key | From (table g, row r, cell c, stack index s) |
|---|---|
| → (LTR) / ← (RTL) | Cell c+1 in row r, s clamped; stays put at the row end. Not handled when the region is list-shaped. RTL is `getComputedStyle(region).direction === 'rtl'`, as at UXC:228. |
| ← (LTR) / → (RTL) | Cell c−1, s clamped. |
| ↓ | s+1 if the cell has another stop. Otherwise the next row, in the cell whose `data-ux-col` is nearest (index clamped when either side lacks it), s=0. From a table's last row, the next table's first row. From the last table, stays put. |
| ↑ | s−1 if s>0. Otherwise the previous row, aligned the same way, at the **last** stop of that cell; it crosses back into the previous table. |
| Home / End | First or last cell of the row, s=0. When list-shaped: the region's first or last stop. |
| Ctrl+Home / Ctrl+End | The region's first or last stop. |
| PgDn / PgUp | The next or previous table, the same row index clamped, the aligned cell, s=0. |
| Enter, Space | `activate(stop, event)`. Skipped when the target is a native control (`button, a[href], input, select, textarea`) or `event.repeat` is set. |
| letters, Escape | The `keys` map. Never on `event.repeat`. |

**Focus model.** The controller keeps, per region: `key`, `pos {g,r,c,s}`, `pending` and `owned`. A module-level `lastInputWasKeyboard` is set by a capture-phase `keydown` and cleared by a capture-phase `pointerdown` or a passive `wheel`.

- **R1. Tab stops.** Every stop gets `tabindex=-1` and the active stop `0`. A region with stops always has exactly one `0`.
- **R2. Choosing the active stop on refresh**, in this order:
  1. the `pending` key;
  2. the current key (a key naming a cell that now holds cards resolves to the first card);
  3. `initial()`;
  4. `pos`, clamped g → r → c → s;
  5. the first stop without `aria-disabled="true"`;
  6. the first stop.
- **R3. `focusin`.** On a stop, or inside one: it becomes the active stop, `pos` is stored and `owned=true`. On the region, or on a non-stop such as Show more: `owned=true` and the active stop is unchanged.
- **R4. Losing ownership.** `owned=false` after a `focusout` whose `relatedTarget` is outside the region (not null), or after a capture-phase `pointerdown` outside the region. A `focusout` with a null `relatedTarget` (the node was removed) keeps it.
- **R5. Restoring.** After every refresh: if `owned` and the keyboard is nowhere (`activeElement` is body, null or disconnected), focus the active stop, or the region when there are no stops. Always `focus({preventScroll:true})`. Then call `scrollIntoView?.({block:'nearest', inline:'nearest'})` **only if `lastInputWasKeyboard`**.
- **R6. When refresh runs.**
  - The page calls `nav.refresh()` at every exit of `renderGridWithSlots` (PAGE:1859-1950), `renderBudgetSidebar` (PAGE:1107-1213) and `renderUnplacedList` (PAGE:2131-2200).
  - A `MutationObserver({childList, subtree})` on the region catches every other redraw: `resetWorkspace` (PAGE:1496-1513) and `clearCopyWorkspaceBoards` (PAGE:912).
  - Attribute writes are not observed, so `applyCardTones` (PAGE:3372) and `markSelection` (PAGE:2103-2112) never move focus.
- **R7. Moving the keyboard.** A key move focuses the new stop, then scrolls it into view (`nearest`).

##### Global CSS (GCSS, next to `.ux-tabs` at :2137)

```css
/* A row of an ARIA grid drawn on a CSS grid: it groups its cells for assistive technology and leaves placement to the table. */
.ux-grid-row { display: contents; }
/* A roving grid cell takes an inset ring: tiled cells and a clipped table never hide it. */
html body [role="gridcell"][tabindex]:focus-visible { outline-offset: -2px !important; box-shadow: none !important; }
/* The keys of a keyboard region, shown while the keyboard is in it (UXControls.roving's hint). */
.ux-keys { position: sticky; inset-block-end: 0; z-index: 3; margin: 0; padding: 6px 10px;
  background: var(--surface); border-top: 1px solid var(--line-strong); color: var(--t2); font-size: var(--text-xs); }
```

- `html body` beats the `!important` in `*:focus-visible` (GCSS:4576-4580). The same precedent is at GCSS:1911.
- **Job card (global, UXC):** a `ResizeObserver` on each `.ux-job` sets `--ux-job-block` on `:root`. It holds the card's block size plus 16px while the card is not `[hidden]`, and `0px` otherwise.

##### Markup (PAGE, TPL)

**`_renderSingleGrid(container, slots, placements, className, secKey, kind)`** (PAGE:1960)
- New 6th parameter `kind`: `'lecture' | 'english' | 'lab'`. The five call sites are PAGE:1896 (lecture), 1899 (lab), 1929 (lecture), 1934 (english) and 1943 (lab).
- **Table:** `div.tw-grid-table` gets `role="grid"` and `aria-label="${gridLabel(secKey, kind)}"`. The **first table of the board only** also gets `aria-describedby="twGridKeys"`.
- **Header row:** `div.ux-grid-row[role=row]` holds:
  - the corner `div.tw-grid-header.tw-grid-corner[role=columnheader]`, containing `<span class="visually-hidden">Day</span>`;
  - the slot headers `div.tw-grid-header[role=columnheader][aria-label="Slot ${i+1}, {t}"]`, with their inner markup unchanged.

  The corner stays child 1 of the row, so the frame test's `.tw-grid-header:nth-child(2)` (tests/test_timetable_workspace_frame_browser.py:57) is still the first slot.
- **Day rows:** one `div.ux-grid-row[role=row]` per day.
  - The day label is `div.tw-grid-slot-label[role=rowheader]`. In English only, it gets `aria-label` with the full day name.
  - Each cell is `div.tw-cell[role=gridcell]` with `data-ux-col="${minutes(slot.start)}"` and `data-focus-key="cell:${secKey||'-'}:${kind}|${day}|${slot.start}"`. The drag handlers are unchanged (PAGE:1987-1993).
- **Cards** (`createPlacementCard`, PAGE:2036):
  - `role="button"` and `data-focus-key="placement:${p.id}"`. **Page code sets no `tabindex`; the controller owns it.**
  - In a cell with more than one card, each card gets `<span class="visually-hidden">(${i+1} of ${n} at this time)</span>`.
- **`.tw-cell:last-child`** (TWCSS:235) now matches the last cell of every row, which is the rule's intent.

**Left list.**
- `renderBudgetSidebar`, per term:
  - `h3.tw-term-head#twTerm-${t}`;
  - then `div.tw-term-list[role=grid][aria-labelledby="twTerm-${t}"]`, with `aria-describedby="twListKeys"` on the first one only;
  - then rows: `div.ux-grid-row[role=row] > div.tw-section-item[role=gridcell][data-focus-key="place:${c.course_key}|${label}"]`.
- Complete rows (PAGE:1159-1170) also get `aria-disabled="true"`.
- The English row's `button.tw-pattern-action` (PAGE:1199-1205) gets `tabindex="-1"`. It stays for the pointer.
- `renderUnplacedList`:
  - one `div.tw-term-list[role=grid][aria-labelledby="twLeftTitle"][aria-describedby="twListKeys"]`;
  - rows keyed `ts:${s.term_section_id}`;
  - Show more (PAGE:2190-2195) stays a native button after the grid. Before it re-renders, its click calls `listNav.active('ts:'+items[visibleLimit].term_section_id)`.
- Empty states (PAGE:1114-1117, 2147-2149) draw no grid.

**Template.**
- `#twGrid` (TPL:245) and `#twSectionList` (TPL:226) get `tabindex="-1"`.
- After `#twGridWrap` (TPL:236), inside `#twCentre`:
  - `<p id="twGridKeys" class="ux-keys" hidden>`
  - `<p id="twBoardStatus" class="visually-hidden" role="status" aria-live="polite"></p>`
- In `#twLeft`, after `#twSectionList`: `<p id="twListKeys" class="ux-keys" hidden>`.

**Page CSS (TWCSS).**
- **Severity ring kept under focus:**
  - `.tw-card { --tw-ring: 0 0 #0000 }`;
  - `.tw-card.critical { --tw-ring: inset 0 0 0 2px var(--sev-critical); box-shadow: var(--tw-ring) }`, and the same for `.warning` (TWCSS:312-313);
  - `html body .tw-card:focus-visible { outline-offset: 1px !important; box-shadow: var(--tw-ring), 0 0 0 4px var(--brand-glow) !important }`.
- `.tw-section-list .tw-section-item { box-shadow: inset 0 0 0 1px var(--line-strong) }` (raised specificity, so a mouse focus no longer erases the row edge at GCSS:4573).
- `.tw-centre`:
  - `scroll-padding-block-start: 52px`;
  - `scroll-padding-inline-start: 60px` (the day column is 56px, TWCSS:199);
  - `scroll-padding-block-end: calc(var(--ux-job-block, 0px) + 40px)`.
- `.tw-workspace[data-busy="true"]` replaces `[aria-busy="true"]` (TWCSS:343-344). PAGE:108 sets `dataset.busy`.

**Page JS wiring** (at top level; `?.` because four jsdom suites load PAGE without UXC):

```js
const boardNav = window.UXControls?.roving?.($('twGrid'), { stop: '.tw-card', hint: $('twGridKeys'),
  initial: () => TW.selectedPlacementId ? `placement:${TW.selectedPlacementId}` : '',
  activate: stop => stop.matches('.tw-card') && selectCardFromKeyboard(stop),
  keys: { Escape: () => clearSelectionFromKeyboard() } });
const listNav = window.UXControls?.roving?.($('twSectionList'), { hint: $('twListKeys'), activate: cell => activateListRow(cell) });
```

- `selectCardFromKeyboard(card)` calls `card.click()` (PAGE:2089-2097) and announces "selected". Focus stays on the card.
- `clearSelectionFromKeyboard()`:
  - if no section is selected, returns `false`;
  - otherwise sets `TW.selectedPlacementId = null`, calls `markSelection()` and `redrawSelection()`, and announces "cleared".
- `activateListRow(cell)`:
  - complete row → announce "all placed";
  - English row → `cell.querySelector('.tw-pattern-action')?.click()`;
  - otherwise → announce the drag hint. 5f replaces this.
- `announceBoard(text)` writes `#twBoardStatus` (clear, then set).
- `selectBoard`: when `TW.loadedBoardId !== boardId` (PAGE:1034), call `boardNav?.reset()`.
- **Locate:** `highlightWorkspacePlacements(ids)` (PAGE:2922) calls `boardNav?.active(firstCard.dataset.focusKey)` without focus. The Issues click handler (PAGE:2545-2556) then announces "located". MRI hotspots locate silently.
- **Panes** (PANES:71-80):

```js
const side = (event.key === '[' || (event.code === 'BracketLeft' && !event.shiftKey)) ? 'left'
  : (event.key === ']' || (event.code === 'BracketRight' && !event.shiftKey)) ? 'right' : '';
if (!side || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
/* field and dialog guards unchanged (PANES:74-76) */
const holds = panes[side].el?.contains(document.activeElement);
toggle(side, holds && state[side] === 'open');   // collapsing the pane that has the keyboard moves it to its rail (PANES:53-60)
```

Update PANES' header comment (lines 7-11).

##### States

| State | Board / list keys |
|---|---|
| No scenario | `#twGrid` is `d-none` (PAGE:1504), so it has no stops. The list shows its empty text. |
| Board loading or failed load | Keys still work on what is drawn. A failed first load hides the grid (PAGE:1029-1032), and hidden stops are skipped. |
| Empty board | Every cell is a stop. Enter does nothing. |
| Published, job, locked, external | Navigation and Enter work. Actions arrive in 5c and 5e. |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Table names (`gridLabel`) | Lectures · English · Labs; with groups: "Section S2 · Lectures" | المحاضرات · الإنجليزية · المعامل؛ الشعبة S2 · المحاضرات |
| Corner | Day | اليوم |
| Slot header label | Slot 1, {t} | الفترة 1، {t} |
| Row header (English only) | Sunday … Thursday | — (`DAY_LABELS` are already full names, PAGE:36-38) |
| Stack | (2 of 3 at this time) | (2 من 3 في هذا الوقت) |
| `#twGridKeys` | Arrows: times, days and the sections at one time · Page Up/Down: next table · Enter: select · Esc: clear the selection | الأسهم: الأوقات والأيام والشعب في الوقت نفسه · Page Up/Down: الجدول التالي · Enter: التحديد · Esc: إلغاء التحديد |
| `#twListKeys` | Arrows: sections · Page Up/Down: next term · Enter on an English row: choose its weekly pattern | الأسهم: الشعب · Page Up/Down: المستوى التالي · Enter على صف الإنجليزية: اختيار نمطها الأسبوعي |
| Selected | {c} selected; its details are in Selection. | حُدِّدت الشعبة {c}؛ تفاصيلها في تبويب «التحديد». |
| Cleared | Selection cleared. | أُلغي التحديد. |
| Located | Located: {day} {t}, {c}. The board's tab stop is on it. | عُثر عليها: {day} {t}، {c}. والتنقل بالمفتاح Tab إلى الجدول يصل إليها. |
| All placed (Enter) | Every section of {c} is placed. | وُضعت كل شعب {c}. |
| Drag hint (Enter, 5a only) | Drag the section onto the board to place it. | اسحب الشعبة إلى الجدول لوضعها. |
| Selection empty (PAGE:2622, English only) | Select a section on the grid to inspect it. | (unchanged) |

##### Tests

**Global: `tests/frontend/timetable-accessibility.test.cjs`, new section "Roving".** It uses a bare fixture:
- grid A: rows SUN and MON, `data-ux-col` 480 / 570 / 660; A[SUN][0] holds two stops `.c`;
- grid B: `data-ux-col` 420 / 480 / 900;
- a list-shaped region of two one-column grids;
- an `#outside` button.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| R1 | Exactly one `[tabindex="0"]`. ↓ walks card1 → card2 → MON cell, and ↑ comes back to card2. → keeps the clamped s. Home sets s=0. | Old tabindex not reset; no stack walk; ↑ enters at the first card; Home keeps s; ←/→ swapped |
| R2 | ↓ from A[MON][660] lands on B's cell at 480. PgDn keeps the row. Ctrl+End and Ctrl+Home reach the ends. | Index alignment; ↓ does not cross; PgDn resets the row; Ctrl ignored |
| R3 | With `region.style.direction='rtl'`, ← goes to the next column. | No mirroring |
| R4 | After a redraw that moves `p:10`, the new node is focused and holds the only 0. When `p:10` is deleted, the stop at `pos` is focused. Focus on `#outside` stays put. `active('p:11')` before `p:11` exists takes effect once it appears. After a `pointerdown`, a restore calls `focus({preventScroll:true})` and **no** `scrollIntoView` (spy). | No observer; `owned` ignored; fallback to the first stop; pending key dropped; scrolls on a passive restore |
| R5 | `letterOf`: {ة, KeyM} → M; {m, Semicolon} → M; {`,`, KeyM} → ''; {p, KeyR} → P; {1, KeyM} → ''. `keys.M` never fires with Ctrl, Shift, Alt or `repeat`. Enter on a div stop clicks once; a repeated Enter does not click; Enter on a native `<button>` stop gets no synthetic click. A handler returning false leaves `defaultPrevented` false. | Code-only; key-only; punctuation through code; repeat guard removed; native skip removed; return value ignored |
| R6 | List-shaped: Home and End go to the region's first and last stop; ← and → are not `defaultPrevented`; an `aria-disabled` cell is reachable; `[hidden]` and `.d-none` stops are skipped. | Home is a no-op; hidden or d-none included |
| R7 | After focus moves to an outside button, and that button and the active node are removed, `restore()` focuses the active stop. | `restore()` no-op |
| R8 | `hint.hidden` is false after a keyboard focus into the region and true after focus leaves; it stays hidden after a mouse focus. | Hint never shown; shown on mouse focus |
| K1 (panes, harness with PANES) | {ج, BracketLeft} toggles `data-left`; {د, BracketRight} toggles `data-right`; {`[`, KeyF, shift} toggles left. {`{`, BracketLeft, shift}, repeat, Ctrl, a target in `#twSectionSearch`, and an open `<dialog open>` do nothing. | Key-only; code-only; shift guard removed; repeat guard removed |
| K2 | With a list stop focused, ج collapses the left pane and focus lands on `#twLeft .tw-rail`. | `toggle(side, false)` |

**Page: `tests/frontend/timetable-workspace.test.cjs`.** The harness (lines 18-63) gains `notify.info`.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| W1 | One `[role=grid]` labelled "Lectures" with `aria-describedby="twGridKeys"`. The header row has the corner "Day" and "Slot 1, 09:00–10:15". There are 5 rowheaders ("Sunday"…) and 5 gridcells with `data-ux-col="540"`. The card has `role=button`, `data-focus-key="placement:10"` and the only `#twGrid [tabindex="0"]`. With two groups, only the first table has `aria-describedby`. | Rows dropped; "Column" wording; tabindex 0 on every card; `describedby` on every table |
| W2 | Arabic: "المحاضرات", "الفترة 1، \u206609:00–10:15\u2069", "اليوم". | No isolation |
| W3 | With card 10 focused, `applyCardTones()` keeps the same node. Card 10 moves to MON and card 11 is added at SUN; after `renderGrid()`, `activeElement` is `placement:10` inside `[data-day=MON]`. Two cards in one cell carry "(1 of 2 …)" and "(2 of 2 …)". | Key built from the cell; `refresh` and the observer removed; stack span missing |
| W4 | Enter on the card: `TW.selectedPlacementId === 10`, `#twTabSelection.active`, focus still on the card, and the selected text in `#twBoardStatus`, **also with `#twWorkspace[data-right=collapsed]`**. Escape clears and announces; a second Escape is not `defaultPrevented`. | Enter unwired; announced in `#twInspectorStatus`; Escape always handled |
| W5 | Budget CS101 (complete), CS323 (2 left), ENG101 (another term): two grids labelled `#twTerm-N`, one stop, CS101 `aria-disabled` and reachable. Enter on ENG101 calls a stubbed `chooseEnglishWorkspacePattern` once. The pattern button has `tabindex="-1"`. | English button left in the tab order; Enter routes nowhere |
| W6 | With CS323 S1 focused, a budget arrives with `used_sections:1`; focus is on the row now at that index. With 125 unplaced rows, Show more focuses `ts:<row 101>`. | No position fallback; no pending key on Show more |
| W7 | An Issues row clicked with `detail:0`: focus stays on the row, the card holds tabindex 0, and the "located" text is announced. | Focus moved to the card; tab stop not moved |
| W8 | `.tw-workspace` never has `aria-busy`; `data-busy` is `true` during a deferred write. | `aria-busy` restored |

**Browser: new `tests/test_timetable_workspace_keyboard_browser.py`.**
- It uses a helper `tests/timetable_workspace_browser_support.py`, extracted from the frame test's `setUp` and `_load` (frame test :118-189). Both files import it. The read-only class keeps the "no non-GET request" check.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| B1 | 1440×790, en and ar: Tab from the left rail lands on the board's tabindex-0 stop, which matches `:focus-visible`. Tab leaves `#twGrid`; Shift+Tab returns to the same stop. → (en) / ← (ar) moves to the next column's `data-start`; its x is larger in en and smaller in ar. | Mirroring removed; tabindex 0 everywhere |
| B2 | CDP `Accessibility.getFullAXTree` at 1920: grid "Lectures"; at least 6 rows; columnheader "Slot 1, …"; rowheader "Sunday"; gridcell; a button containing "AI111"; grids in the list. Guards Chrome's exposure of `display:contents`. | `role=row` removed; rows set to `display:none` |
| B3 | Keyboard into the board, → , then `page.evaluate("selectBoard(TW.boardId)")`: the same key is focused on a new node that matches `:focus-visible`. Then click a card, scroll `.tw-centre` to the bottom and call `renderGrid()`: `scrollTop` is unchanged. A clicked, selected card keeps its solid `--t0` outline. | No restore; scroll on a passive restore; `--tw-ring` and selected rules removed |
| B4 | CDP keyDown {ج, BracketLeft, 219} toggles `data-left`; {د, BracketRight, 221} toggles `data-right`. | `event.key`-only test |
| B5 | 1366×650, en and ar, wide font: with SUN's row 20px under the sticky header, ↑ from MON: the target's top is at or below the header's bottom, and `elementFromPoint` at its centre is inside it. The header is ≤ 52px. Repeat with `.ux-job` shown and a stop near the bottom: the stop's bottom is above the card's top. | `scroll-padding` removed; `--ux-job-block` unused |

**Manual acceptance (recorded under "As built"):** NVDA 2025 + Chrome, en and ar. Tab into the board; arrows, Enter, Esc, `[`. The arrows must move cards, not the virtual cursor. Repeat with JAWS if available.

##### Risks

1. **`display:contents` rows.** B2 guards Chrome. Fallback: rows become `display:grid; grid-template-columns: subgrid; grid-column: 1/-1`, and the header row (not its cells) becomes sticky; that changes the frame test's sticky assumptions.
2. **Focus mode in screen readers.** If the manual check fails, the fallback is: cells become the stops (`stop: null`), each card-holding cell gets `aria-labelledby` set to its cards and `aria-current` when selected, and Enter on a stacked cell steps into the cards. Decide this before 5c.
3. **Focus rings.** Every page rule that must survive keyboard focus (the severity ring) needs `html body` plus `!important`, because of GCSS:4576-4580.
4. **Single letters.** They act only while focus is on a card (WCAG 2.1.4).
5. **Cost.** One childList observer per region; about 400 nodes walked per key press on a six-group board. Nothing measurable next to a `renderGrid`.

---

#### 5b: placing a planned section from the list

##### Server

**`compute_scenario_budget`** (SVC:3600-3666): each row gains `"used_section_labels": sorted(used_sections.get(key, set()), key=natural)`. The set is already built at SVC:3627-3632.

**`create_planned_section_placements`** (SVC:1982), inside the atomic block after `get_or_create` (SVC:2033):
- If the section is `not _created`, has `source_tag == "tw_planned"`, has no `SectionPlacement` in the scenario, and every meeting row has `instructor == ""`, delete its `TermSectionMeeting` rows before the loop at SVC:2060.
- Otherwise behave as today.

##### Page

**`renderBudgetSidebar`:**
- Labels come from `freeSectionLabels(c)`: the first `remaining_sections` of `S1, S2, …` that are not in `used_section_labels` (case-insensitive). This replaces PAGE:1172-1173.
- `dragstart` (PAGE:1183-1197) adds `required_meetings_per_section` (SVC:3660).

**`onDrop` `create_planned`** (PAGE:2301-2326):
- If `payload.required_meetings_per_section > 1` and the section is not English: `notify.warning(multiDropWords)` and return. No request is sent.
- Otherwise POST with `course_key` and `course_name` added.
- Pass `_onError` mapping `FULL_SECTION_PATTERN_REQUIRED` (VIEWS:1911-1919) to the same `multiDropWords`.

##### Strings

**Multi-meeting drop.**
- English: "{c} meets {n} times a week, and its meetings are placed together; a drop places one. Nothing was placed."
- Arabic: «يُدرَّس {c} {times}، وتُوضع لقاءاته معاً، والإفلات يضع لقاءً واحداً. لم يُوضع شيء.»
- `{times}` uses `pluralAr`: two «مرتين أسبوعياً» · few «{n} مرات أسبوعياً» · many «{n} مرةً أسبوعياً» · other «{n} مرة أسبوعياً».

##### Tests

| Test | What it checks | Mutants it must kill |
|---|---|---|
| P1 (pytest, new `tests/test_timetable_planned_create.py`) | `used_section_labels` for S1, S10, S3 placed on two boards is `['S1','S3','S10']`. | Active board only; lexical sort; field missing |
| P2 | Create S2 with pattern A, remove its placements, create S2 with pattern B: its meetings are exactly B. A section with a placement elsewhere, or with an instructor on a meeting row, keeps its rows. | No replacement; replacement despite placements; replacement despite an instructor |
| J1 (jsdom) | A one-meeting drop's body has `course_key` and `course_name`. | Identity omitted |
| J2 | A two-meeting drop sends no POST and shows the exact words in en and ar ("مرتين"). | Old POST sent; `plural()` used instead of `pluralAr` |
| J3 | A 400 `FULL_SECTION_PATTERN_REQUIRED` shows the same words and no `notify.error`. | `_onError` returns false |
| J4 | With S1 and S3 used and 3 planned, the row reads S2. | `S${used+1}` |

##### Risks

- The meeting-row replacement touches registrar-free rows only; the `tw_planned` and blank-instructor guards keep it so.

---

#### 5c: dialog layer, Change room, R

##### Global

**`UXControls.dialog`** (UXC):

```
open(dialog, {initialFocus, returnFocus}) -> boolean
```
- Returns false when a helper dialog is already open.
- Calls `UXControls.close()` for menus, then `showModal()`; without it, sets `[open]` plus `data-ux-dialog="fallback"`.
- Focuses `initialFocus()`, else the first focusable element.
- Remembers `returnFocus` in a WeakMap.

```
close(dialog, reason = 'close')
```
- Does nothing for a dialog the helper did not open.
- Closes the dialog, then focuses `returnFocus()` if that element is connected, enabled and not inside `[hidden]` or `.d-none`.
- Dispatches `ux-dialog-close` with `{reason}`.

```
isOpen(dialog)
```

Delegated listeners on `document`, acting **only on dialogs in the WeakMap**:
- `click` on `[data-dialog-close]` → close with reason `'cancel'`;
- capture-phase `cancel` → `preventDefault`, then close with `'cancel'`;
- capture-phase native `close` while the dialog is still in the map (Chrome's close watcher) → do the close bookkeeping;
- in fallback mode, `keydown` Escape not `defaultPrevented` → close.

**Choice list** (one delegated `keydown` in UXC):
- For a radio inside `.ux-choice-list[data-ux-submit]`, among radios of the same name that are not hidden (filtered in JS, no `CSS.escape`):
  - Home, End, PgUp and PgDn (±10, clamped) check, focus and `scrollIntoView?.()` the radio, then dispatch a bubbling `change`.
  - Enter: `preventDefault`, then `form.requestSubmit()` once, and only when the form's submit button is enabled.
- The arrow keys stay native.

**CSS (GCSS, after `.ux-choice` at :2373):**
- `.ux-dialog-scroll[open]`: a flex column with `overflow: hidden`; its `> form` is a flex column with `min-height: 0`; `.ux-dialog-body` has `min-height: 0`, `overflow: auto` and `overscroll-behavior: contain`. The head and foot stay put.
- `.ux-choice-list`: `display: grid; gap: 6px; margin: 0; padding: 0; list-style: none`.
- `.ux-choice-list[aria-busy="true"]`: `opacity: .6`.
- `.ux-choice[data-tone]`: `border-inline-start: 3px solid` in `--sev-critical`, `--sev-warning` or `--sev-info`; `ok` uses `color-mix(in srgb, var(--sev-ok) 55%, transparent)`, as `.ux-stat` does at GCSS:2205-2207.
- `.ux-choice-static`: `cursor: default`.
- `.ux-choice-title` is a flex line (gap 6px).
- `.ux-choice-status`: `color: var(--t3); font-size: var(--text-xs); margin: 0`.
- `.ux-dialog-foot-note`: `margin-inline-end: auto; color: var(--t2); font-size: var(--text-xs)`.
- `@media (forced-colors: active) { .ux-choice[data-tone] { border-inline-start-color: CanvasText } }`.

**Row ARIA:**
- the radio has `aria-labelledby` pointing at its title span and `aria-describedby` pointing at its verdict and facts spans;
- tone icons are `aria-hidden`; the verdict is visible text.

Docs: §3.3 adds `UXControls.dialog`, `.ux-dialog-scroll`, `.ux-choice-list` / `-static` / `-status`, `.ux-keys` and `--ux-job-block`. §5 adds: "Dialogs opened through `UXControls.dialog` resolve `returnFocus` at close".

##### Shared module: `static/js/timetable-room-choice.js` → `TimetableRoomChoice` (TPL: before PAGE, `?v=1`)

- **`codeKey`, `occupied`, `fits`** are SPLIT's rules, copied as they are: `roomCodeKey`, `roomIsOccupied` and `roomFitsSection` (SPLIT:4243-4269).
- **`verdict(room, p)`** returns `{group, tone, words}`. The first rule that applies wins:

  | Condition | Group | Tone | Words |
  |---|---|---|---|
  | not `fits` | misfit | — | — |
  | an `occupied_by` entry has `board_id === p.board_id` | inUse | critical | `TONE_LEAD.critical` + `ISSUE_KINDS.room` |
  | other `occupied_by` entries | inUse | warning | warning lead + `roomElsewhere` |
  | `department_fit === false` | free | info | "For context: outside this board's programme rooms" |
  | otherwise | free | ok | "Free and fits" |

  The verdict reads `occupied_by` only; same-class copies are already left out by the server.
- **`misfitReasons(room, target, ar)`**, **`factsHtml(room, target, ar)`** and **`words(ar)`**.

##### Server

**`preview_placement_room_candidates`** (SVC:2138-2391):
- The occupiers loop (SVC:2236-2261) skips `other` when `other.term_section_id == placement.term_section_id` and its day, start and end equal the target's. These are the copies `assign_physical_meeting_room` moves together (SVC:2694-2750).
- Each `occupied_by` entry gains `"term_section_id"`.
- After the cap (SVC:2383): if no returned candidate has `_room_key(room_code) == _room_key(placement.room)`, append every candidate with that key.
- `_PlacementTimeCheck` is untouched, so the 18ba3f8 oracle (tests/test_tw_room_candidates_fast_path.py:399-433) is unaffected.

##### Page

**Selection's Room field** (PAGE:2662), drawn through `UXControls.redraw`:

```html
<div class="tw-sel-field"><dt>Room</dt><dd><bdi>{placementRoomText(p) || 'Not set'}</bdi>
 [<button type="button" class="btn btn-sm btn-neutral tw-sel-inline" data-tw-action="room" data-focus-key="room"
   data-placement-id="{id}" aria-haspopup="dialog" aria-label="{Change room|Choose a room}"
   [disabled data-tw-hold aria-describedby="twSelActionNote"]>{Change…|Choose…}</button>]</dd></div>
```

- No button when the gate code is `online` or `noRoom`.
- When the action is held, `<p class="tw-sel-help" id="twSelActionNote">` shows the unique held reasons.
- `syncSelectionActions()` runs at the end of `syncWorkspaceControls` (PAGE:90-114). For each `#twRightBody [data-tw-action]` it recomputes the gate and updates `disabled`, the hold and the note text in place.
- One delegated `click` on `#twRightBody` for `[data-tw-action]` calls `TW.keyActions[kind](p, {origin: 'sel:'+kind+':'+id})`.

**Keys:**
- `boardNav` gains `keys.R: card => runCardKey('room', card)`.
- `#twGridKeys` gains "R: change the room" / "R: تغيير القاعة".

**Template:**

```html
<dialog id="twRoomDialog" class="ux-dialog ux-dialog-scroll" aria-labelledby="twRoomTitle" aria-describedby="twRoomSub">
 <form method="dialog" id="twRoomForm" novalidate>
  <div class="ux-dialog-head"><div><h2 class="ux-dialog-title" id="twRoomTitle"></h2><p class="ux-dialog-sub" id="twRoomSub"></p></div>
   <button type="button" class="btn btn-sm btn-neutral btn-icon" data-dialog-close aria-label="Close|إغلاق">…× as TPL:280-282…</button></div>
  <div class="ux-dialog-body" id="twRoomBody" tabindex="-1"></div>
  <p id="twRoomStatus" class="visually-hidden" role="status" aria-live="polite"></p>
  <div class="ux-dialog-foot"><p class="ux-dialog-foot-note" id="twRoomNote"></p>
   <button type="button" class="btn btn-sm btn-neutral" data-dialog-close>Cancel|إلغاء</button>
   <button type="submit" class="btn btn-sm btn-primary" id="twRoomAssign" data-tw-mutation data-tw-hold disabled aria-describedby="twRoomNote">Assign room|تعيين القاعة</button></div>
 </form></dialog>
```

**Body (`roomDialogHtml`, drawn with `UXControls.redraw($('twRoomBody'), html)`), in order:**
1. "Current room".
2. An optional time-clash hint, from `candidates[0].validation.critical_count`.
3. An optional "also changes on N other boards": the `occupied_by` entries of the current room with the same `term_section_id`. Since the server now skips them, this count comes from the move answer's `placements` after the commit; before the commit, use the selected section's other-board placements in `TW.placements` when they are known, else leave the hint out.
4. A "this meeting only" hint when `p.meetings.length > 1`.
5. The filter: `input#twRoomFilter[type=search][dir=auto][aria-controls=twRoomChoices]`.
6. `#twRoomChoices` with two fieldsets, each a `.ux-choice-list[data-ux-submit]` (it must have that attribute): "Free and fits (n)" and "Fits, but in use at this time (n)" plus its hint.
7. A misfit disclosure (`data-ux-disclosure="tw-room:misfits"`, closed by default; forced open when nothing fits) over `.ux-choice-static` rows.
8. A truncation note.

Rows:
- Radios are named `twRoomChoice` with `value=room_code` and `data-focus-key="room-choice:CODE"`.
- They are de-duplicated by `codeKey`; the first in server order wins.
- `UNASSIGNED` is never drawn.

**Keyboard in the dialog:**
- On open, focus `#twRoomBody` (`aria-busy` on the list).
- When the answer arrives, if focus is still on the body, focus the checked radio (the current room when it fits) or the first one.
- Tab order: filter, list, misfit toggle, Cancel, Assign.
- Filter:
  - typing hides rows in place by code, building or wing, with no redraw;
  - a hidden chosen row clears the choice;
  - Enter with exactly one visible row checks and focuses it, and never submits;
  - Escape with text clears the field and calls `stopPropagation` and `preventDefault`, so the dialog stays open.

**Gate and hold:**
- `openRoomDialog(p, {origin})`: gate first (A); then the open guard; then `UXControls.dialog.open(…, {initialFocus: () => $('twRoomBody'), returnFocus: () => resolveOrigin(origin)})`.
- `syncRoomDialog()` (O(1); a no-op while closed) sets `data-tw-hold` when any of these is true: the state is not `ready`; nothing is chosen; the choice is the current room; a request is pending; the dialog is blocked.
- Then `disabled = hold || !workspaceCanEdit()`.
- The note shows the first reason in this order: published, job, busy, "Choose a room.", "This is the current room."

**Read:** `previewRead('room|'+id, '/ops/tw/placements/'+id+'/room-candidates/')` with **no** query (D20).

**Stale state:** `target.day`, `start` or `end` differ from `p`'s, or the scenario, board or placement changed. The dialog shows the stale words and "Check again", which runs `await selectBoard(TW.boardId)` and then a fresh read. Assign is held.

**Commit:**
1. `oldRoom = p.room ?? ''` (the raw value, `UNASSIGNED` included).
2. `dialogWrite(dialog, '/ops/tw/placements/'+id+'/move/', {room})`.
3. On `{code}`, show it inline: `PLACEMENT_EXTERNALLY_OWNED` (VIEWS:3017-3025) → external, and hold; `SCENARIO_PUBLISHED` → published, and hold; `NOT_FOUND` → not found, and hold; `SCENARIO_BUSY` → busy (the job was already announced by `twFetch`); anything else → "The room was not changed."
4. On `{data}`:
   - push `{type: 'room', placement_id, old_room, new_room}` and clear `TW.redoStack`;
   - `UXControls.dialog.close`;
   - `boardNav.active('placement:'+id)`;
   - `await selectBoard`, then `updateUndoRedoButtons()`;
   - toast. The clash list is `occupied_by` of the chosen row, without the ids in `data.placements`. Mention the other boards when `data.placements` holds any.
5. **Undo and redo** (PAGE:2393): `if (action.type === 'room') return twFetch(`/ops/tw/placements/${id}/move/`, post({room: undo ? action.old_room : action.new_room}))`. The body never carries a time.

##### States

| State | Selection / R | Dialog |
|---|---|---|
| Published, loading, job | Held, with its reason | — |
| Another scenario's section | Held; reason "external" | A stale flag answered by 409: an inline alert, Assign held, no undo entry |
| Online / no room needed | No button; R → reason | `target.is_online` or `no_room_required` (SVC:2157-2223): that message, Assign held |
| Time-locked | **Offered**; the subtitle adds the lock words (PAGE:2056) | The commit body is unchanged |
| A write in flight or a board refresh | Offered | The list loads; Assign is held with "busy" and comes back on by itself |
| Loading / error / empty / none fit / truncated | — | Each has its own words below. On error, Retry reads again (fresh). |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Button (visible) | Change… / Choose… | تغيير… / اختيار… |
| Button (label) | Change room / Choose a room | تغيير القاعة / اختيار قاعة |
| Title | Change room · {c} {c} | تغيير القاعة · {c} {c} |
| Sub-line | {day} {t} · this meeting only; the section's other meetings keep their rooms. | {day} {t} · هذا اللقاء فقط؛ تحتفظ لقاءات الشعبة الأخرى بقاعاتها. |
| Current room | Current room: {c} · Not set · No room | القاعة الحالية: {c} · لم تُحدَّد · بلا قاعة |
| Time's own clashes | Whatever the room, its time keeps {X}. X = 1 clash / {n} clashes | أيّاً كانت القاعة، يبقى في وقته {X}. X = pluralAr {one:'تعارض واحد', two:'تعارضان', few:'{n} تعارضات', many:'{n} تعارضاً', other:'{n} تعارض'} |
| Other boards | Its room also changes on {X} (the same class at the same time). X = 1 other board / {n} other boards | تتغيّر قاعته أيضاً في {X} (الشعبة نفسها في الوقت نفسه). X = pluralAr {one:'لوحة أخرى', two:'لوحتين أخريين', few:'{n} لوحات أخرى', many:'{n} لوحةً أخرى', other:'{n} لوحة أخرى'} |
| Filter | Filter rooms · placeholder "Room code or building" · "No room matches the filter." | تصفية القاعات · رمز القاعة أو المبنى · لا توجد قاعة تطابق التصفية. |
| Groups | Free and fits (n) · Fits, but in use at this time (n) · Rooms that do not fit (n) | متاحة ومناسبة (n) · مناسبة لكنها مستخدمة في هذا الوقت (n) · قاعات غير مناسبة (n) |
| In-use hint | Choosing one saves a room clash, which Issues then shows. | اختيار إحداها يحفظ تعارض قاعة يظهر بعدها في «المشاكل». |
| Verdicts | Free and fits · For context: outside this board's programme rooms · {TONE_LEAD} + kind label | متاحة ومناسبة · للاطلاع: خارج قاعات برامج هذه اللوحة · (ISSUE_KINDS, PAGE:3194-3209) |
| Occupants | Used by {c} ({board}) at {t}; and 1 more / and {n} more | تستخدمها {c} ({board}) في {t}؛ pluralAr {one:'وشعبة أخرى', two:'وشعبتان أخريان', few:'و{n} شعب أخرى', many:'و{n} شعبةً أخرى', other:'و{n} شعبة أخرى'} |
| Facts | Lecture room · Lab · {n} seats · {n} spare · {n} short · Male (M) · Female (F) · Building B · Wing W · Floor 2 | قاعة محاضرات · معمل · pluralAr seats {one:'مقعد واحد', two:'مقعدان', few:'{n} مقاعد', many:'{n} مقعداً', other:'{n} مقعد'} · spare {zero:'بلا فائض', one:'مقعد فائض', two:'مقعدان فائضان', few:'{n} مقاعد فائضة', many:'{n} مقعداً فائضاً', other:'{n} مقعد فائض'} · short {one:'ينقصها مقعد واحد', two:'ينقصها مقعدان', few:'ينقصها {n} مقاعد', many:'ينقصها {n} مقعداً', other:'ينقصها {n} مقعد'} · طلاب (M) · طالبات (F) · المبنى B · الجناح W · الطابق 2 |
| Misfit reasons | This meeting needs a lecture room · needs a lab · needs a room of type {c} · needs a Male (M) room · needs a Female (F) room | هذا اللقاء يحتاج إلى قاعة محاضرات · إلى معمل · إلى قاعة من نوع {c} · إلى قاعة طلاب (M) · إلى قاعة طالبات (F) |
| Loading, error, busy, retry | Finding rooms for this meeting… · The rooms could not be read. · The server is busy; try again in a moment. · Try again | جارٍ البحث عن قاعات لهذا اللقاء… · تعذّرت قراءة القاعات. · الخادم مشغول؛ أعد المحاولة بعد قليل. · إعادة المحاولة |
| Empty, none fit, truncated | No rooms are on record. · No room fits this meeting. · Showing the best 60 of {n} rooms. | لا توجد قاعات مسجّلة. · لا توجد قاعة مناسبة لهذا اللقاء. · تُعرض أنسب 60 قاعة من أصل {n}. |
| Stale | This meeting changed since its rooms were read. · Check again | تغيّر هذا اللقاء منذ قراءة قاعاته. · إعادة التحقق |
| Notes | Choose a room. · This is the current room. | اختر قاعة. · هذه هي القاعة الحالية. |
| Progress | Assigning room {c}… | جارٍ تعيين القاعة {c}… |
| Toasts | Room {c} assigned. · Room {c} assigned; {c} also uses it at this time. · It also changed on {X}. | عُيّنت القاعة {c}. · عُيّنت القاعة {c}، ويستخدمها في هذا الوقت أيضاً: {c}. · وتغيّرت أيضاً في {X}. |
| Failures | This meeting is no longer on the board. · The room was not changed. | لم يعد هذا اللقاء في اللوحة. · لم تتغيّر القاعة. |

##### Tests

**Global: `timetable-accessibility.test.cjs`.**

| Test | What it checks | Mutants it must kill |
|---|---|---|
| G1 | Fallback mode: open sets `[open]` and honours `initialFocus`; `[data-dialog-close]` and an Escape `keydown` close it; focus goes to the element `returnFocus()` gives **at close**, after the opener node was replaced. | Resolver read at open |
| G2 | A cancelable `cancel` is `defaultPrevented`; the dialog closes; `ux-dialog-close` fires with `reason 'cancel'`. A native `close` without `cancel` runs the same bookkeeping. | No capture listener; close watcher ignored |
| G3 | A disabled or detached target throws nothing and does not take focus. A dialog the helper did not open (`#twGenerateDialog`): its × and Escape behave natively, and the helper's handler never runs. | Listeners not scoped to the helper's dialogs |
| C1 | `.ux-choice-list[data-ux-submit]`: End, Home and PgDn (clamped) check, focus and fire `change`; hidden radios are skipped; Enter submits once (the spy counts `submit` events); Enter with the submit button disabled submits nothing; a list without `data-ux-submit` never submits. | End off by one; no clamp; hidden included; double submit; submit not opt-in |
| F1 | `twFetch` with `_onError` returning true on a 409 `SCENARIO_BUSY` with `active_job`: `tw:planner-job` still fires. | Announce left after `_onError` |
| F2 | A 502 with an HTML body calls `_onError(null, 502)` and fires no toast. A rejected fetch calls `_onError(null, 0)`. | Non-JSON path skips `_onError`; rejection path skips it |

**Module: new `tests/frontend/timetable-room-choice.test.cjs`.** One case table drives `fits`, `occupied`, `verdict` (en and ar) and `misfitReasons`. In `timetable-manual-clash-ruling.test.cjs` (whose harness loads SPLIT), eval the module and assert `fits(r) === roomFitsSection(r)` and `occupied(r) === roomIsOccupied(r)` over the same table. Mutants: `fits` requires `available`; tone taken verbatim from the server.

**Page: `timetable-workspace.test.cjs`.** The harness evals the module.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| RC1 | A locked meeting: the GET URL is exactly `/ops/tw/placements/10/room-candidates/`. Choose R2 and submit: the POST body deep-equals `{room:'R2'}`. The undo top is `{type:'room', old_room:'LAB25', new_room:'R2'}`. A `/boards/2/` GET and a success toast follow. | Slot parameters added to the GET; time or override sent; lock refused; no history push; no `selectBoard` |
| RC2 | R1 free; R9 occupied on board 2; R8 on another board; R7 with `department_fit:false`; R3, R4, R5 misfits: radios R1/R9/R8/R7 with tones ok/critical/warning/info; misfits are static rows with words from their flags. | `free()` used for choosability; server tone used; same-board rule dropped |
| RC3 | Hold: nothing chosen → "Choose a room."; the current room → held; a deferred `toggleLock` POST → disabled with the busy note, back on afterwards. | `data-tw-mutation` dropped; current-room hold dropped |
| RC4 | Opened from Selection, then `renderGrid()` and a Selection redraw with new content: the radio nodes are the same and the dialog stays open; `cancel` puts focus on the **new** `[data-focus-key=room]`. Opened by R from the card: focus ends on the new card. | Opener kept as a node |
| RC5 | After assigning from a card, focus is on the rebuilt card through `boardNav`. | `active()` call removed |
| RC6 | Shared, online, `needs_room:false` and published meetings: no button, or a held one; R gives the exact reason and no GET. A stale shared flag answered by 409: inline alert, Assign held, no undo entry, no toast. | Each gate clause; `_onError` returning false |
| RC7 | Each state: deferred GET; 500 then Retry (a second GET); empty; only misfits (the disclosure opens); total 75 with 60 candidates; `is_online`; `no_room_required`; `target.day` differs → stale, no POST. | One mutant per branch |
| RC8 | Undo and redo POST `{room:'LAB25'}` then `{room:'R2'}`. | Rooms swapped; a time added |
| RC9 | Arabic: the sub-line holds الأحد and `<bdi dir="ltr">09:00–10:15</bdi>`; seats 3, 11 and 1 read '3 مقاعد', '11 مقعداً', 'مقعد واحد'. A meeting with `UNASSIGNED` reads 'بلا قاعة'. | `plural()`; no bdi; raw `p.room` printed |
| RC10 | Filter: "R8" leaves one row and Enter checks it; Escape with text clears and the dialog stays open; a hidden chosen row clears the choice. Enter on a radio posts. | One mutant per rule; keydown handler removed |
| RC11 | Open, Esc, open: one GET. A reopen while the first read is still in flight: one GET. After a write: a second GET. | No memo; abort-and-refetch; key without `dataVersion` |

**Python: `tests/test_timetable_workspace_room_availability.py`.**

| Test | What it checks | Mutants it must kill |
|---|---|---|
| RS1 | Section A is on two boards at SUN 09:00 in R100: A's own copy is not an occupant of R100 and R100 is `available`. A at another time on another board **is** an occupant. B in R200 is listed with `term_section_id = B.id`. | Field dropped; skip without the time check |
| RS2 | 70 rooms with the current room sorting last: it is in `candidates`. | Append removed |
| RS3 | Over a fixture covering all six reasons, `len(reasons)` equals the number of false or true flags (SVC:2293-2305). | A reason without a flag; a dropped reason |

The oracle tests in `tests/test_tw_room_candidates_fast_path.py` stay unchanged and green.

**Browser** (keyboard file, new class `RoomDialogTests`, which may POST; rooms R101 lecture 40, R102 held at the same time on the same board, R103 lab, R104 with 10 seats):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| RB1 | Keyboard only, en and ar: Tab to Selection's Change…, Enter, ↓ to R101, Enter. The database shows room R101, `is_locked` still True, the time unchanged. Focus is back on the button. Esc on a second open also returns there. R on the card, choose, and focus returns to the card. | Locked meetings refused; focus restore removed; `cancel` routing removed |
| RB2 | Choosing R102 saves it; Issues shows "Room double-booked" and the card's ring is critical. | Assign held for in-use rooms |
| RB3 | 1366×650 and 1440×790, light and dark, en and ar: `CONTRAST` on `.ux-choice-title`, `.ux-choice-desc`, `.ux-field-label`, `.ux-dialog-sub`; no text under 11px; the foot stays visible while the body scrolls; in Arabic ↓ moves to the next radio. | `.ux-dialog-scroll` removed; a low-contrast tone colour |

##### Risks

1. **The 60 cap.** The current room is always returned; finding another room past the cap needs the `q=` follow-up.
2. **Rooms with the same code for M and F** are committed by code. Rows are de-duplicated, and a gender misfit stays static.
3. **Copies on other boards** change silently (SVC:2694-2750). The dialog says so before the commit and the toast after it.
4. **In NVDA browse mode `r` means "next radio".** It depends on 5a's focus-mode check.

---

#### 5d: one slot verdict (server)

##### API

| Function | Change |
|---|---|
| `preview_placement_slot_candidates(placement_id, *, include_verdict=False, offer_manual=False)` (SVC:1003) | New keywords |
| `preview_planned_section_slot_candidates(…, include_verdict=False)` (SVC:1374) | New keyword |
| `tw_placement_slot_candidates_view` (VIEWS:2150-2163) | `include_verdict = GET.verdict == "1"`, `offer_manual = GET.offer == "manual"` |
| `tw_board_planned_slot_candidates_view` (VIEWS:1823-1859) | `include_verdict = GET.verdict == "1"` |

- `@preview_lane` is kept on both views.
- Every other caller keeps the defaults, so the legacy output and its order are identical.

##### Verdict shape (both previews; additive)

```
verdict = { severity: 'critical'|'warning'|'info'|'ok', counts: {critical, warning, info},   // counts over ALL items
            students: int, items: [≤ 8], more: int, own_section_meeting?: bool }
item    = { issue_kind, severity, source, meeting: int|null,
            other: {course_code, section, board_id, board_label, day, start, end} | null,
            room: str|null, teacher: str|null, students: int|null, blocked_students: int|null }
candidate.verdict_rank: 1..n    // sorted copy: own_section_meeting last, then severity, counts.critical, counts.warning, counts.info, score, day, start
response.current_verdict        // slot preview only: the current slot, direct items only
```

`issue_kind` is one of `time`, `instructor`, `room`, `blocked`, `softOverlap`, `instructorElsewhere`, `roomElsewhere`, `crossBoard`, `crossBoardMinor`. Items are ordered by severity, then by kind in that order, then by students descending.

##### Item rules

| Source (legacy kind) | Item |
|---|---|
| `same_course` | `time` / critical |
| `students` (same board) | `blocked = 0 if clash_free(masks_after, masks[C]) else shared`; `time`/critical when `blocked >= HARD_OVERLAP_THRESHOLD`, else `softOverlap`/warning (SVC:585-601) |
| `instructor` (same board) | `instructor` / critical |
| cross-board instructor (new for moves; planned already checks it at SVC:1594-1622) | `instructorElsewhere` / warning |
| `room` (same board) | `room` / critical, `room` = this meeting's room (SVC:4163-4166) |
| `cross_board_room` | `roomElsewhere` / warning |
| `cross_board_students` | `crossBoard` / warning when shared ≥ T, else `crossBoardMinor` / info (SVC:4191-4210) |
| `(day, start) in blocked_slot_keys(scenario.blocked_slots)` (timetable_validation.py:191-201) | `blocked` / critical. Flagged, never removed. |
| **H15 effect (moves only)** | For each course C with shared(K, C) ≥ T, where `clash_free(masks_before, masks[C])` and not `clash_free(masks_after, masks[C])`: one `time`/critical item, `source: 'h15_effect'`, for each existing same-board overlapping pair (a section of K that is not the moving placement, a section of C) on **any** board. `other` is C's section; `blocked_students = shared`. |

**Masks** are built in memory from `scenario_placements`, which is already loaded (SVC:1068-1072). This adds no query.
- `section_masks[norm(course_key or code)][ts_id] |= _time_mask(…)`, keyed as `_placed_section_masks` keys them (SVC:381-419).
- `own_rest` = the OR of the masks of this section's other placements, on all boards.
- `masks_after(cm)` = K's masks, with this section's mask replaced by `own_rest | cm`.

**`own_section_meeting`** is true when a placement with the same `term_section_id` on the same board has the candidate's day and start.

**Planned previews** (include_verdict):
- Items are built per meeting cell, with `meeting` set to the meeting index, and copied (`{**item, "meeting": i}`).
- H15 is evaluated per pattern: masks are K's masks plus `[OR of the pattern's meetings]`.
- There are no room items and no effects (adding a section never removes a clash-free pair).
- Blocked meetings are flagged per meeting; `generate_meeting_options` stays without `blocked_slots` (SVC:1836-1840), so split's create-assist output is unchanged.

**Memo (always on):** `score_slot` in the planned preview (SVC:1523) is memoised per `(slot_kind, day, start, end)` for the request. Callers must not mutate the result (`score_pattern` copies, SVC:1741-1745).

**`offer_manual`** (non-policy sections):
- `slots = offered_move_columns(effective_<kind>_slots(scenario), placement.start_time)`;
- `offered_move_columns` is new in `core/services/timetable_slot_config.py`, next to `effective_lecture_slots` (:49-61): it drops `policy_only` columns, and keeps `online_only` columns only when the meeting starts in one;
- its docstring names timetable-slot-config.js:166-169 as the rule it mirrors.

##### Tests (new `tests/test_tw_slot_verdict.py`; fixtures from `tests/one_teacher_support`; demand as in `tests/test_timetable_repair_foundation.py:195-200`)

| Test | What it checks | Mutants it must kill |
|---|---|---|
| V1 | A same-board room clash gives a `room`/critical item; the legacy `tone` stays `risky`. | Mapped to warning; legacy tone changed |
| V2 | Cross-board shared 20 → `crossBoard`/warning; 19 → `crossBoardMinor`/info. | `>=` changed to `>`; critical kept |
| V3 | Same-board shared 20 with a clash-free sibling pair → `softOverlap`. A fixture where only the moved mask removes the last free pair → `time`/critical. | H15 skipped; the old mask used |
| V4 | Moving S1 flips an untouched S2×C overlap on the **same** board and one on **another** board: two `h15_effect` items naming C's sections. | Effects not computed; `masks_before` used as after; same board only |
| V5 | Blocked times are flagged for the candidate and for `current_verdict`. | Check removed |
| V6 | The section's other meeting slot has `own_section_meeting` and is ranked last. | Flag dropped; not ranked last |
| V7 | `verdict_rank` puts an ok candidate with a worse score above a critical one; the `candidates` order is unchanged. | Ranked by score; list reordered |
| V8 | Ten overlaps give counts of 10, 8 items and `more` = 2. | Counting after the cap |
| V9 | `offer=manual` offers evening columns only to a meeting already in one; the default is unchanged; policy columns never. | Always on; never on |
| V10 | Without `verdict=1`, the full JSON equals an oracle captured from `7116f53` on the fixture. Query counts are equal in both modes (`django_assert_num_queries`). Both views keep `@preview_lane`. | Any legacy drift; an extra query |
| V11 (parity) | For three candidates, POST the move and compare `detect_board_conflicts` critical and warning pairs involving the placement or the effect pairs, plus `check_publish_readiness` blocker codes, with the verdict's items. | Any rule drift |
| V12 | Planned: H15 per pattern; the `meeting` index (a clash on the 2nd meeting gives `meeting == 1`); a blocked meeting flagged; cross-board instructor → `instructorElsewhere`. | Per-cell H15; index always 0; blocked excluded |
| V13 | Memo: patching `_time_mask` with `wraps`, a cell no placement uses is computed exactly once across every pattern that uses it. The planned JSON without verdict equals the oracle. | Memo removed; memo key without `slot_kind` |

##### Risks

- **Cost on the 0.5 vCPU host.** The masks and effects are only computed with `include_verdict`. Effects are checked only for courses with shared ≥ T, and pairs are enumerated only on a flip.
- The cross-board instructor severity is a warning; this follows §7.1 #3 and can be revisited.

---

#### 5e: Move and M

##### Markup

**Selection** (PAGE:2669): before Lock, and not for English sections (they have "Change section pattern", PAGE:2658):

```html
<button type="button" class="btn btn-sm btn-neutral" data-tw-action="move" data-focus-key="move"
 data-placement-id="{id}" aria-haspopup="dialog" [disabled data-tw-hold aria-describedby="twSelActionNote"]>Move…</button>
```

**Template:** `#twMoveDialog` has the same shell as `#twRoomDialog`: ids `twMoveTitle`, `twMoveSub`, `twMoveBody` (tabindex −1), `twMoveStatus` (`role=status`, visible as `.ux-choice-status`), `twMoveNote`, and `twMoveCommit` (`data-tw-mutation data-tw-hold disabled aria-describedby="twMoveNote"`).

**Body:**
1. The reference: a `.ux-choice.ux-choice-static[data-tone]` "Now" row, built from `current_verdict`.
2. `<fieldset class="ux-field"><legend class="ux-field-label">…</legend><div class="ux-choice-list" data-ux-submit aria-busy>` with rows.
3. An `[role=alert]` line and Retry.

Rows:
- Radios are named `twMoveChoice` with `value="DAY|start"` and `data-focus-key="move-choice:DAY|start"`.
- Labels read `{day} <bdi dir="ltr">start–end</bdi>`.
- The verdict line is `TONE_LEAD[severity]: unique ISSUE_KINDS labels`, or "No clash found".
- At most three detail lines, then "and N more".
- `own_section_meeting` rows are static, with their reason.
- `ISSUE_KINDS` gains `instructorElsewhere` (warning) at the end (PAGE:3194-3209).

**Detail lines** (escaped; `{c}` in bdi):

| Kind | Line |
|---|---|
| time, softOverlap | `label: {c} · {day} {t} · studentsNeedBoth(n)` (PAGE:3218-3220) |
| h15_effect | `label: {c} · {day} {t} · blockedNoPair(n)` |
| instructor, instructorElsewhere | `label: {teacher} — {c}[ ({board})] · {day} {t}` |
| room, roomElsewhere | `label: {room} — {c}[ ({board})] · {day} {t}` |
| crossBoard, crossBoardMinor | `label: {c} ({board}) · {day} {t} · studentsNeedBoth(n)` |
| blocked | `label` |

##### Behaviour

**`TW.keyActions.move = openMoveDialog`.** `boardNav.keys.M` is `card => runCardKey('move', card)`. `#twGridKeys` gains "M: move" / "M: النقل".

**`openMoveDialog(p, {origin})`:**
1. Gate (A).
2. If the section is English or another policy section: announce "Finding patterns…" and `await chooseEnglishWorkspacePattern({type:'move', placement_id})`. In that function (PAGE:2209-2255):
   - after a successful switch, `boardNav?.active('placement:'+(rows.find(r => r.day === placement.day) || rows[0]).id)` before `selectBoard` (PAGE:2246);
   - after `policy.choose` resolves, `boardNav?.restore()`.
3. Open guard, then `UXControls.dialog.open`, with `returnFocus` from `resolveOrigin(origin)`.

**Read:**
- `previewRead('slot|'+id, '/ops/tw/placements/'+id+'/slot-candidates/?verdict=1&offer=manual')`.
- 404 → not found; `SERVER_BUSY` after `twFetch`'s retries → busy; anything else → error with Retry.
- Rows are drawn in `verdict_rank` order. The first choosable row is checked; if focus is still on the body, it moves to that radio.

**Commit:**
1. Snapshot `{scenarioId, boardId, id, day, start, end}` at open. At commit, a change in any of them, or a new lock or external flag, gives inline words and no POST.
2. `commitPlacementMove(id, {day, start_time, end_time}, {dialog})` is extracted from PAGE:2338-2374 and used by `onDrop` too:
   - POST the body exactly as `{day, start_time, end_time}`;
   - push the undo entry with drag's shape (PAGE:2363-2365);
   - clear the redo stack.

   Drag calls it without a dialog: its errors follow `twFetch` as today.
3. Dialog error codes, shown inline:
   - `PLACEMENT_LOCKED` → locked, and hold;
   - `PLACEMENT_EXTERNALLY_OWNED` → external, and hold;
   - `PLACEMENT_DUPLICATE` → own-section words;
   - `SCENARIO_PUBLISHED` → published, and hold;
   - `SCENARIO_BUSY` → busy;
   - `NOT_FOUND` → not found;
   - `NETWORK` and anything else → "The move was not saved.", then `selectBoard(at.boardId)` behind the dialog, after which the stale guard stops a second commit.
4. On success: close; `boardNav.active('placement:'+id)` (only when the origin was a card; from Selection the key is the `sel:` origin); `await selectBoard`; `updateUndoRedoButtons()`; `reportEdit(movedWords(day, time), [id])`. When the verdict had a `room` item, add the R hint.

**Busy while open:**
- A `tw:planner-busy` listener (PAGE:111) is active only while the dialog is open and not committing.
- It runs `syncMoveDialog()`: the note gives the hold reason; Commit goes off through the sweep at PAGE:106 and comes back on afterwards.

##### States

| State | What happens |
|---|---|
| Locked | Selection's Move… is held with its reason; M gives a toast; no GET. |
| Another scenario's section | Same, with the external words. |
| English or policy section | The chooser, with no second Move… button. |
| Online in an evening column | Other evening columns are offered (D14). |
| Published or job | Held. |
| A write in flight | Opens; Commit is held until it ends. |
| Loading, empty, error | Its words below. |
| Escape during a commit | The dialog closes; the result arrives as a toast (D17). |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Button | Move… | نقل… |
| Title | Move {c} {c} | نقل {c} {c} |
| Sub-line | This meeting only: {day} {t}. The section's other meetings stay where they are. + Its room stays {c}. / It is online and needs no room. / It has no room yet. | هذا اللقاء فقط: {day} {t}. تبقى لقاءات الشعبة الأخرى في أماكنها. + وتبقى قاعته {c}. / وهو عن بُعد ولا يحتاج إلى قاعة. / وليست له قاعة بعد. |
| Now / legend | Now · Other times on the grid, best first | الآن · أوقات أخرى في الجدول، الأفضل أولاً |
| Loading, empty, error | Finding times… · No other time of this kind is on the grid. · The times could not be loaded. | جارٍ البحث عن الأوقات… · لا يوجد وقت آخر من هذا النوع في الجدول. · تعذّر تحميل الأوقات. |
| No clash | No clash found | لا تعارض |
| Own-section row | Another meeting of this section is at this time. | لقاء آخر لهذه الشعبة في هذا الوقت. |
| blockedNoPair(n) | leaves 1 student / {n} students with no clash-free pair of sections | pluralAr {one:'لا يجد طالب واحد شعبتين بلا تعارض', two:'لا يجد طالبان شعبتين بلا تعارض', few:'لا يجد {n} طلاب شعبتين بلا تعارض', many:'لا يجد {n} طالباً شعبتين بلا تعارض', other:'لا يجد {n} طالب شعبتين بلا تعارض'} |
| instructorElsewhere | Teacher also teaching on another board | المدرّس يدرّس في لوحة أخرى في الوقت نفسه |
| More | and 1 more / and {n} more | pluralAr {one:'وبند آخر', two:'وبندان آخران', few:'و{n} بنود أخرى', many:'و{n} بنداً آخر', other:'و{n} بند آخر'} |
| Chosen (foot note) | Chosen: {verdict text} | المختار: {verdict text} |
| Commit, progress | Move to this time · Moving… | نقل إلى هذا الوقت · جارٍ النقل… |
| Changed, not saved | This section changed while the list was open. Nothing was moved. · The move was not saved. | تغيّرت هذه الشعبة أثناء فتح القائمة. لم يُنقل شيء. · لم يُحفظ النقل. |
| Report (shared with drag) | Moved to {day} {t}. [· {TONE_LEAD}: {labels}] | نُقلت إلى {day} {t}. [· {TONE_LEAD}: {labels}] |
| R hint | Press R on the card to change its room. | اضغط R على البطاقة لتغيير قاعتها. |
| English | Finding patterns… | جارٍ البحث عن الأنماط… |

##### Tests (jsdom: `timetable-workspace.test.cjs`)

| Test | What it checks | Mutants it must kill |
|---|---|---|
| M1 | One GET `…/slot-candidates/?verdict=1&offer=manual`. Rows follow `verdict_rank`, not array order. A candidate with legacy `tone:'avoid'` and verdict `ok` reads "No clash found". | Legacy tone rendered; array order kept |
| M2 | The commit body equals `{day, start_time, end_time}`. The undo entry deep-equals drag's. Undo POSTs the old time. `onDrop`'s move sends the same body through `commitPlacementMove`. | Room sent; no undo push |
| M3 | Locked (not shared): no request, the locked words, Move… disabled with `data-tw-hold`, surviving `syncWorkspaceControls()`. Shared: the external words. | Hold dropped; external checked after the lock |
| M4 | English: the policy chooser (stubbed `choose`) is called and no move dialog opens; Selection has no Move…. In a published scenario, M on an English card gives the published toast (the gate runs first). After a stubbed switch, `boardNav` holds the new row's key. | Gate after English routing; no `active()` after the switch |
| M5 | A deferred write in flight: the dialog opens, Commit is held with the busy note, and comes back on afterwards. | Opening refused while busy |
| M6 | M on an **unselected** card while card 11 is selected, then Esc: focus is on that card. Same from the selected card. | Resolver prefers Selection |
| M7 | Committing: before the POST, `activeElement` is the checked radio; the status has no `[aria-busy=true]` ancestor. | No focus step; status inside the busy list |
| M8 | A 400 `PLACEMENT_LOCKED`, a 409 external and a 409 duplicate show inline words; `notices` stays unchanged while the dialog is open. Esc during a deferred commit closes the dialog and the result arrives as a toast. | Toast while open; Escape blocked |
| M9 | The placement's time changes while the dialog is open: the changed words and no POST. A network rejection: "not saved" plus a board GET. | Stale check removed; no reload |
| M10 | Arabic: every label time sits in `bdi[dir=ltr]`; `more(2)` reads 'وبندان آخران'; a course code `<img>` is shown as text. | No bdi; no escaping |

**Browser** (keyboard file, class `MoveDialogTests`; fixture adds MATH201, locked and **not** shared):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| MB1 | en, ar × light, dark × 1366×650, 1440×790: open Move for AI111 from Selection; `CONTRAST` on `.ux-choice-title`, `.ux-choice-desc`, `.ux-dialog-sub`, `.ux-choice-status`; no text under 11px; Commit is in the viewport while only the body scrolls; in Arabic every `bdi[dir=ltr]` matches `^\d\d:\d\d–\d\d:\d\d$`; Esc returns to `[data-focus-key=move]`. | Scroll shell removed; bdi removed; focus restore removed |
| MB2 | en, ar: M on the AI111 card; choose the radio by **accessible name** `/Mon.*13:00/` (a same-board R101 clash with AI222); it reads critical "Room double-booked" / "القاعة محجوزة مرتين"; Enter sends exactly one POST `{"day":"MON","start_time":"13:00","end_time":"14:15"}`; the card at MON 13:00 gets `critical` and is focused; Undo POSTs SUN 09:00. | Legacy tone; room sent; focus not on the moved card |
| MB3 | MATH201: Move… is disabled with its note visible; no `/slot-candidates/` GET is sent. | Lock check removed |

##### Risks

1. The kept room can make a time rank last (critical). The sub-line says the room stays, and the report gives the R hint.
2. The English chooser's own gaps (D16) remain until the recorded follow-up.
3. Undo after the meeting is locked fails with `PLACEMENT_LOCKED`, as drag's undo does today.

---

#### 5f: Place…

##### Markup

**List rows** (5a structure). On each placeable budget row's gridcell:
- `aria-haspopup="dialog"`;
- a head `<span class="tw-section-head"><span class="code">{code} <bdi>{label}</bdi></span><span class="tw-place-hint" aria-hidden="true">Place…</span></span>`;
- a meta part `{n} meetings a week` when n > 1.

The English row's nested button (PAGE:1199-1205) is **removed**; the row itself is the control.

Pointer: one listener on `#twSectionList` reacts to a click on `.tw-place-hint`. A click elsewhere on the row stays a drag affordance.

Page CSS:
- `.tw-section-head { display: flex; gap: 6px; align-items: baseline }`;
- `.tw-place-hint { margin-inline-start: auto; color: var(--text-brand); font-size: var(--text-xs); font-weight: 600; visibility: hidden }`;
- visible on `:hover` and `:focus-visible`;
- `[draggable="false"] .tw-place-hint { display: none }`.

`activateListRow` (5a) becomes:
- complete row → announce;
- English → `chooseEnglishWorkspacePattern({...c, type: 'create_planned', section_label})`;
- unplaced (`ts:`) row → announce the "needs a budget" hint;
- otherwise → `TW.keyActions.place(request, {origin: cell.dataset.focusKey})`.

`#twListKeys` becomes "Arrows: sections · Page Up/Down: next term · Enter: place the section (opens a dialog)".

**Template:** `#twPlaceDialog` has the 5c shell (`twPlaceTitle`, `twPlaceSub`, `twPlaceBody`, `twPlaceStatus`, `twPlaceNote`, `twPlaceCommit`). The body holds:
1. a term note (`role=note`, warning icon) when `programme_term ≠ board.nominal_term`;
2. `<fieldset class="ux-field"><legend class="ux-field-label">Must include</legend><div class="ux-field-row fb-fit"><div class="fb-search fb-sm"><label class="fb-lbl" for="twPlaceDay">…</label><select id="twPlaceDay">` plus `twPlaceTime`, built from the candidates' meetings (Any, then days; Any, then distinct start–end with " · Lab");
3. a fallback note;
4. `.ux-choice-list[data-ux-submit]` rows: radios named `twPlaceChoice` with `value=sig` (the meetings sorted by day, `DAY@start-end|…`; duplicates dropped, the best rank kept) and `data-focus-key="place-choice:sig"`. The title lists each meeting `{day} {t}[ · Lab]`; then the verdict line and details as in Move, with "{day} {t}:" before each item that has a `meeting` index;
5. alert and Retry.

##### Behaviour

**`openPlaceDialog(request, {origin, includes = null})`.** `request` = `{course_code, course_key, course_name, section_label, capacity: max_per_section || 40, required, programme_term}`.
- Guards: no board → `notify.warning`; then the gate parts that apply (published, loading, job); then the open guard.
- Read: `previewRead('planned|'+boardId+'|'+key+'|'+label, '/ops/tw/boards/'+boardId+'/planned-slot-candidates/?course_code&course_key&section_label&limit=1000&verdict=1')`.
- Filter on the client: rows are hidden in place, no redraw. If the checked row is hidden, the first visible row is checked. With `includes` and no row matching, the filter is cleared and the fallback note shown.

**Drop** (`onDrop`, PAGE:2301):
- `create_planned` with `required_meetings_per_section > 1` (and not English) → `openPlaceDialog(request, {origin: 'place:'+key+'|'+label, includes: {day, start, end}})`. Nothing is posted. This replaces 5b's refusal.
- A `FULL_SECTION_PATTERN_REQUIRED` answer to a one-meeting drop → the same.

**Commit:**
1. Context check: if the scenario or board changed → the "board changed" words.
2. `dialogWrite(dialog, '/ops/tw/placements/create-planned/', {board_id, course_code, course_key, course_name, section_label, capacity, meetings: rows.map(({day, start, end}) => ({day, start_time: start, end_time: end}))})`.
3. Codes, shown inline:
   - `FULL_SECTION_PATTERN_REQUIRED` → read again (fresh) and give the needs-pattern words;
   - `PLACEMENT_DUPLICATE` → `await loadBudget()`, take the next free label, retitle, and say so; if none is left, the all-placed words and hold;
   - `SCENARIO_PUBLISHED` → hold;
   - `SCENARIO_BUSY` → busy;
   - `VALIDATION_PLANNED_PATTERN` and `NETWORK` → "Nothing was placed."
4. On success:
   - push `{type: 'section_create', placement_ids, board_id, payload, meetings: TimetableMeetingPolicy.snapshot(rows)}` and clear redo;
   - close the dialog (focus returns to the origin row by key);
   - `await selectBoard`. The list redraw drops the origin key, so 5a's R2 position rule puts the keyboard on the row now at that index: the course's next section, or the next course;
   - `updateUndoRedoButtons()`;
   - `highlightWorkspacePlacements(ids)`;
   - `reportEdit(placedWords, ids)`.

**History** (PAGE:2393-2433):
- **Undo** of `section_create`: POST `/remove/` with `{}` for each id in reverse order, skipping ids already in a `removed_placements` answer (policy bundles). If any removal fails, set `placement_ids` to the ones left and return null. `runHistoryAction` reloads the board when some succeeded, and the action stays on the undo stack.
- **Redo:** POST `create-planned` with the payload and all meetings, then `remapHistory` (POSTED at PAGE:2428; POLICY:32-48).

##### States

| State | What happens |
|---|---|
| Loading, empty, filter fallback, error with Retry | Each has its own words below. |
| Job or write in flight | Opens; Commit is held. |
| Published | Refused on opening (toast); held when it comes back from the server. |
| A lock elsewhere | Never refuses a placement; the clash is in the verdict. |
| Pattern with blocking clashes | Never disabled and never confirmed (owner rule); the foot note states the verdict. |
| English rows | The chooser. |
| Online course (`OnlineCourseLookup` via the board payload) | A foot note: no room is booked (Q2). |
| Unplaced rows | Keyboard placement is not available; Enter announces why (follow-up F4). |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Row hint | Place… | وضع… |
| Row meta | {n} meetings a week | pluralAr {two:'لقاءان أسبوعياً', few:'{n} لقاءات أسبوعياً', many:'{n} لقاءً أسبوعياً', other:'{n} لقاء أسبوعياً'} |
| Title | Place {c} {c} | وضع {c} {c} |
| Sub-line | {board} · {n} meetings a week · the whole weekly pattern / {board} · one meeting a week | {board} · {meetings} · النمط الأسبوعي كاملاً / {board} · لقاء واحد أسبوعياً |
| Term note | This course is in term {t}; this board is term {b}. | هذا المقرر في المستوى {t}، وهذه اللوحة للمستوى {b}. |
| Filter | Must include · Day · Time · Any day · Any time · Lab | يشمل · اليوم · الوقت · أي يوم · أي وقت · معمل |
| Legend | Complete weekly patterns, best first / Times, best first | الأنماط الأسبوعية الكاملة، الأفضل أولاً / الأوقات، الأفضل أولاً |
| Loading | Ranking the patterns… / Ranking the times… | جارٍ ترتيب الأنماط… / جارٍ ترتيب الأوقات… |
| Status | {patterns(n)}, best first · {clean(n)} / {k} of {n} include {day} {t} | {patterns(n)}، الأفضل أولاً · {clean(n)} / {k} من {n} تشمل {day} {t} |
| patterns(n) | — | pluralAr {zero:'لا أنماط', one:'نمط واحد', two:'نمطان', few:'{n} أنماط', many:'{n} نمطاً', other:'{n} نمط'} |
| clean(n) | {n} with no clash | pluralAr {zero:'لا نمط بلا تعارض', one:'نمط واحد بلا تعارض', two:'نمطان بلا تعارض', few:'{n} أنماط بلا تعارض', many:'{n} نمطاً بلا تعارض', other:'{n} نمط بلا تعارض'} |
| times(n), one meeting | — | {zero:'لا أوقات', one:'وقت واحد', two:'وقتان', few:'{n} أوقات', many:'{n} وقتاً', other:'{n} وقت'} |
| Fallback | No pattern includes {day} {t}; showing every pattern. | لا يوجد نمط يشمل {day} {t}؛ تُعرض كل الأنماط. |
| Empty | No complete pattern fits this board's teaching columns. Check Edit time slots… and blocked times. | لا يوجد نمط كامل يناسب أعمدة التدريس في هذه اللوحة. راجع «تعديل الفترات الزمنية…» والأوقات المحجوبة. |
| Error | The patterns could not be ranked. | تعذّر ترتيب الأنماط. |
| Commit, progress | Place section · Placing the section… | وضع الشعبة · جارٍ وضع الشعبة… |
| Foot notes | Rooms are chosen afterwards (R on a card). To change one meeting later, use Move…. / Online for this board: no room is booked. | تُختار القاعات بعد الوضع (R على البطاقة). ولتغيير لقاء واحد لاحقاً استخدم «نقل…». / عن بُعد في هذه اللوحة: لا تُحجز قاعة. |
| Needs a pattern | This course now needs {n} meetings a week; the patterns were ranked again. | يحتاج هذا المقرر الآن إلى {m}؛ أُعيد ترتيب الأنماط. m = pluralAr genitive {two:'لقاءين أسبوعياً', few:'{n} لقاءات أسبوعياً', many:'{n} لقاءً أسبوعياً', other:'{n} لقاء أسبوعياً'} |
| Duplicate | Section {c} was placed meanwhile; this is now {c}. | وُضعت الشعبة {c} في هذه الأثناء؛ هذه الآن {c}. |
| All placed, board changed, nothing placed, no board | Every section of {c} is placed. · The board changed; nothing was placed. · Nothing was placed. · Create a board first. | وُضعت كل شعب {c}. · تغيّرت اللوحة؛ لم يُوضع شيء. · لم يُوضع شيء. · أنشئ لوحة أولاً. |
| Unplaced row (Enter) | Drag this section onto the board to place it; keyboard placement needs a section budget. | اسحب هذه الشعبة إلى الجدول لوضعها؛ الوضع بلوحة المفاتيح يحتاج إلى ميزانية شعب. |
| Report | Placed {c} {c}. [· {TONE_LEAD}: {labels}] | وُضعت {c} {c}. [· {TONE_LEAD}: {labels}] |

##### Tests (jsdom: `timetable-workspace.test.cjs`)

| Test | What it checks | Mutants it must kill |
|---|---|---|
| PL1 | Enter on a row: one GET with `verdict=1&limit=1000`. Choosing rank 2 posts **all** its meetings plus `course_key` and `course_name`, and no `credit_hours`. | First meeting only; rank 1 sent instead of the choice; identity dropped |
| PL2 | A two-meeting drop on THU 13:00 sends no POST; the dialog opens with THU and 13:00–14:15 selected and only matching rows visible. No match → the full list plus the note. | Old POST; `includes` ignored; no fallback |
| PL3 | A pattern with critical items is enabled, posts, and its foot note states the verdict. | Disabled; confirmation added |
| PL4 | Arabic: times isolated in rows and options; "3 أنماط", "11 نمطاً", "190 نمط". | No isolation; English plurals |
| PL5 | Success: the origin key is gone and focus is on the row now at that index; the new cards pulse. | `active()` or position rule bypassed; no highlight |
| PL6 | Undo removes every id in reverse; a failing middle remove keeps the remaining ids on the stack **and** reloads the board. Redo posts `create-planned` with all meetings and remaps the ids. | A single remove; pop regardless; no reload on partial failure; `/create/` redo |
| PL7 | `PLACEMENT_DUPLICATE`: the dialog stays open, relabels, and focus is on the checked radio. `FULL_SECTION_PATTERN_REQUIRED`: a fresh GET. | Toast and close; no re-read |
| PL8 | An English row has no nested button and Enter opens the chooser (stub), not `#twPlaceDialog`. | English routed to Place |
| PL9 | A reopen while the read is in flight sends one GET. | Abort and refetch |

**Browser** (keyboard file, class `PlaceDialogTests`; AI222 S2 needs two meetings; the left pane opened first):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| PB1 | Keyboard only, en and ar: Tab to the list, ↓ to AI222 S2, Enter; the checked radio is focused; End and Home work; Enter sends exactly one POST with 2 meetings. The ORM shows two S2 placements; the grid shows 6 cards; focus is in the list; no page errors. | Enter, Home or End handler removed |
| PB2 | Drag onto THU 13:00 opens with the filter set; Esc; nothing is sent. | Old POST |
| PB3 | 1366×650 and 1440×790, light and dark, en and ar: the dialog and its Commit are in the viewport and only the body scrolls; no text under 11px; `CONTRAST` passes; in Arabic, the first "09:00" lies left of "10:15". | `dir="ltr"` removed; the whole dialog scrolls |

##### Risks

1. **Payload size.** About 160 patterns, each with up to 8 items: a few hundred KB on the 0.5 vCPU host. The memo (D15) and the single read keep the server work bounded; record the size in "As built".
2. **The generator offers only same-index patterns** (timetable_autoplace.py:418-454). Other combinations need Move… per meeting afterwards.
3. **A misleading server code.** The "one meeting per day" refusal is also coded `FULL_SECTION_PATTERN_REQUIRED` (VIEWS:1913-1918, SVC:1975-1976). Place re-reads, which is harmless.

---

#### 5c-audit (server): room candidates report the quality audit's rule

**Why.** Issues' room rows ("Room too small", "Room unsuitable") come from the
quality audit: `build_meeting_demands` says what a meeting needs and which rooms
are compatible, and the recovery evaluator's `_original_room_report` judges the
rooms it holds. Change room's candidates had a rule of their own: seats from
`registered_count`, else `available_capacity`, else the board's target; a room
with no department or no gender fits; only this board's pool counts. So the
dialog could save a room Issues then flag, and refuse one Issues accept. On the
QA copy's scenario 665: 72 choosable rows the audit calls too small, and 580
rows refused for seats the audit accepts.

**Server (this PR; no client change).**
- `core/services/timetable_room_audit.py` holds the rule, moved out of
  `timetable_exact_rooming` and `timetable_recovery_rooming` unchanged:
  - `meeting_room_need`: the type, the buffered budget seats (40 with no budget
    row, reported as `capacity_source: "default"`), the gender (boards that
    disagree raise) and one programme pool per board;
  - `room_misfits`: the one compatibility predicate (type, exact gender, every
    board's pool), behind every `MeetingDemand.compatible_rooms`;
  - `meeting_room_verdict`: missing, split, undersized, unsuitable, incompatible;
  - `MeetingRoomAudit.choice(room)`: what the audit says once that room is saved.

  It loads no OR-Tools and imports neither the workspace nor exact rooming
  (`ExactRoomingError`, `_hhmm` and `_to_min` are re-exported there).
  `build_meeting_demands`, `_load_room_inventory`, `_original_room_report` and
  quality's `_rooms` call it, and their outputs are identical to `a5d8f96`.
- `preview_placement_room_candidates(..., include_audit=False)`; the view sets
  it from `?audit=1` (exactly "1"). Without it, the answer and its query count
  are byte-identical to `a5d8f96` (the oracle
  `tests/fixtures/tw_room_audit_legacy.json`, ids mapped to fixture symbols),
  and the internal callers (bulk clean rooms, repair) keep the default.
- With it:
  - `target.audit` = `{assessed, save: "room_only" | "slot_change",
    required_type, required_capacity, capacity_source, required_gender,
    boards: [{board_id, board_label, programmes}], placement_ids,
    moving_placement_ids, kept: [{placement_id, board_id, board_label, room}],
    current}`, or `{assessed: false, reason}` with `duplicate_room_codes`,
    `online` (online on this board), `no_room_required` (a policy course that
    takes no room), `unknown_day`, `invalid_interval` (also a time the audit
    cannot read, such as `9:00:00`) or `boards_disagree_on_gender`. Every
    answer to `?audit=1` carries `target.audit`, so a missing key means a
    server older than this PR (the client then keeps its legacy rule);
  - each candidate gains `audit` = `{compatible, undersized, reasons,
    programme_board_ids, quality_reasons}`. `reasons`, in order: `type`,
    `gender`, `programme`, `capacity`, `split` (another copy of the class stays
    in another room), `unroomed_copy`. `quality_reasons` are exactly the
    meeting's quality reasons after the save, `clash` aside (tested by saving
    every room of every subject and reading `_rooms`);
  - `current` is the audit's answer for the current room (also a room the
    inventory does not know), `null` when the placement has none.
- The meeting after the save follows the move view: a room-only save moves the
  copies in the placement's room (`_copies_moving_with`, now shared with
  `assign_physical_meeting_room`); a slot change moves the placement alone.
- The listing in audit mode: the legacy 60 and the current room, then every
  other room the audit does not refuse for gender, in rank order. The legacy
  score ranks a fitting room in use below type and gender misfits, so the cap
  could hide a choosable room. `summary` is unchanged. This settles F6 for the
  dialog.
- Cost: one query (the class's copies), plus the gender of each other board
  in the meeting (two queries at most each), plus, for a policy course that
  uses a shared room, the budget read the request skips (its room type is
  fixed; no policy uses a shared room today); nothing grows with the room
  count.
  Measured on the QA copy (this machine), within the 10% budget:
  - scenario 665's four sample placements, best of 31: +0.7 to +1.6 ms
    (+2% to +6%) on 19 to 31 ms, with one teacher per section off and on;
    17 → 18 and 33 → 34 queries; peak memory +0.09 MiB;
  - every one of its 187 placements, mean of 3 interleaved passes (two
    runs): 20.5–21.6 → 22.0–23.4 ms (+7.0% to +8.2%) with one teacher off,
    32.5–34.0 → 33.7–35.1 ms (+3.0% to +3.7%) with it on; 52.3 → 57.9 rows
    per answer (the listing past the cap); peak memory 2.93 → 2.98 MiB.

  The audit pays one predicate call per meeting and room:
  `build_meeting_demands` 10.6 → 11.8 ms and quality's `_rooms` 15.6 → 16.4 ms
  (a generator in the predicate had cost +3.5 ms; it is a plain loop). That is
  a little over the 1 ms budget, and harmless: the optimiser builds the
  demands a few times per run (local search, v2, compaction, cap repair), not
  per search step.
- On real data: on every scenario of the QA copy (662–666, 705 meetings
  the audit judges), the answer's need equals the audit's, every listed
  room's `compatible` equals the audit's domain, and the current room's
  `quality_reasons` equal Issues' rows: no mismatch.

**D20c, amended (the client PR applies it):** "Rooms that do not fit, judged by
the quality audit's requirement when the answer carries one (`?audit=1`), are
listed with their reason and cannot be chosen, except a room short of seats,
which is a choosable warning (Q1). Split view keeps the legacy rule until F3."
On 665 the two pages then differ on 1,679 rows that the dialog offers and
split view refuses or does not list (seats), and on none the other way: type
and gender agree everywhere on the QA data. Under the legacy seat number, 72 rows the
dialog offered were "Room too small" to Issues, and 580 it refused were
big enough.

**Q1, restated and decided** (tech owner, delegated, 2026-10-22). Judged by the
audit, should a room short of the audit's seat need (the buffered budget
demand, or 40 when the course has no budget row) or of the wrong type be
choosable as a warning?
- **Seats: yes.** A choosable warning row, "Room too small", which Issues then
  show. The need is an estimate: a buffered forecast, and the default 40 is a
  policy number, not the section's size.
- **Type: no.** Refused (D20c): a lab meeting in a lecture room has no
  equipment.
- **Gender: no.** Refused: rooms are segregated; this is not a quality trade-off.

So the client refuses the audit reasons `gender` and `type` only. The server's
extended listing keeps every row not refused for gender, so this answer (or a
later change to it) needs no server change.

**Known limits.**
- A scenario-wide audit failure (a gender conflict, a bad interval or an
  unknown day in another meeting) is not seen per meeting; the client reads
  `TW.quality.rooms.assessed`.
- Candidates find occupants with a 5-minute mask and compare times as stripped
  strings; the audit uses exact minutes and `HH:MM`. `09:00:00` against `09:00`
  is one meeting to the audit and an occupant to the candidates (follow-up:
  `_same_class` becomes `meeting_key` equality, after #175).
- A slot change describes the meeting joined, not the one left.
- The default-40 need, shared meetings, boards without a programme and rooms
  without a department have no instance in the QA data; their parity rests on
  the fixture (`tests/tw_room_audit_support.py`).
- A policy course that uses a shared room is skipped as an occupant by the
  candidates but judged by the audit, which then reads the budget itself (the
  legacy fields read none for a policy course). No policy uses a shared room
  today; a test patches the English one to.

**Follow-ups.** The client PR (`feat/workspace-change-room-audit`); F3 (split
view on the module with `?audit=1`); bulk clean rooms and repair accepting only
`quality_reasons == []`; `_PlacementTimeCheck`'s room warning for a shared
section's own copies; online identity by display code everywhere.

---

#### Follow-ups (recorded, not in PR 5)

- **F1.** Rebuild the English chooser (`TimetableMeetingPolicy.choose`) on `UXControls.dialog` and `.ux-choice-list` for both pages: `pluralAr` counts, visible disabled reasons, `returnFocus` by key, verdict words.
- **F2.** Move the Generate dialog onto `UXControls.dialog`. Its native focus return is pinned at tests/test_timetable_workspace_frame_browser.py:220-250.
- **F3.** Split view:
  - labels from `used_section_labels` (SPLIT:1635);
  - room functions delegating to `TimetableRoomChoice` (SPLIT:4243-4288);
  - the slot verdict in its create-assist.
- **F4.** Keyboard placement for unplaced rows (scenarios without a budget). This needs `meetings[]` handled atomically in the non-policy branch of `tw_placement_create_view` (VIEWS:2077-2147).
- **F5.** A bulk remove endpoint, so undo of a section create is atomic.
- **F6.** A room-candidates `q=` filter beyond the 60 cap. For the dialog, 5c-audit lists past the cap every room the audit does not refuse for gender.
- **F7.** Keys for Delete (Remove) and Shift+F10 (a card action menu).

#### Owner questions (not settled by the owner rules)

- **Q1.** Change room by hand: should a room **short of seats** or **of the wrong type** become a choosable warning row ("Room too small" / "Room unsuitable" are already warnings, PAGE:3203-3204), leaving only the wrong gender refused? PR 5 keeps split view's refusal (SPLIT:7095-7098). The rule is one function, `TimetableRoomChoice.fits`. **Restated and decided in 5c-audit** (2026-10-22, tech owner, delegated): judged by the quality audit, seats short of its need are a choosable warning; wrong type and wrong gender stay refused.
- **Q2.** Place for an **online** course: offer only the online (evening) windows? The 2026-07-28 rule for GS/GSE (timetable_autoplace.py:275-284) points that way. PR 5 offers the daytime columns, as the server does today (SVC:1428-1431), and shows a note.
- **Q3.** Change room acts on **one meeting**, as split view does. Should it offer "all N meetings of this section" when they share the current room?
