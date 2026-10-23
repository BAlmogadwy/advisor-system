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
  closed. After each toggle the button fires a bubbling `ux-disclosure`
  event (`{open, key}`, key null without `data-ux-disclosure`): showing rows
  changes no node, so a region whose keyboard depends on what is shown hears
  it from the event. A toggle without the key keeps no choice (the board's
  folds keep their own, per board and text). (Section planning's `.sp-chev`
  predates it and is a candidate to move onto it.)
- `UXControls.roving(region, options)` (and `UXControls.letterOf(event)`):
  the WAI-ARIA grid pattern for a region of `[role=grid] > [role=row] >
  [role=gridcell]` — one tab stop, arrows (mirrored in RTL; ↑/↓ also walk the
  stops stacked in one cell), Home/End, Ctrl+Home/End, PageUp/PageDown between
  tables, Enter/Space to activate, a map of letter keys (by `letterOf`: the
  Latin letter typed, or the key's letter where it types another script's).
  It keeps the active stop (by `data-focus-key`) through any redraw and gives
  the keyboard back when it was in the region; a restore scrolls only after a
  key. It reads its stops again on a redraw (`childList`), on a
  `ux-disclosure` event inside it, and when the page calls `nav.refresh()`
  after hiding or showing rows itself — never on attributes (a tone, a dim),
  so they never move the keyboard. A stop inside `[hidden]` is no stop, and a
  stop's `tabindex` is the controller's alone. `.ux-grid-row` (display:
  contents) is a row on a CSS grid; a roving cell takes an inset focus ring;
  `.ux-keys` is the keys line shown while the keyboard is in a region.
  `--ux-job-block` is the floating job card's height, for scroll padding.
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
- `UXControls.dialog` (`open(dialog, {initialFocus, returnFocus})`,
  `close(dialog, reason)`, `isOpen(dialog)`): one layer for the template
  dialogs that choose something (Move, Place, Change room). Modal through
  `showModal`, or `[open]` plus `data-ux-dialog="fallback"` where there is
  none (jsdom); one at a time (`open` answers false while one is open); menus
  close first; focus to `initialFocus()`, else the first control that takes
  it (never a hidden input). Delegated listeners close it, as `'cancel'`, from
  its `[data-dialog-close]` buttons (never a submit, whatever their type; one
  with no type is given `type="button"` at open, so Enter in a text field
  never clicks it as the form's default button), the
  browser's `cancel` (stopped, then closed by the helper) and, in fallback
  mode, an Escape no control inside has used. A native close no cancel began
  (a `<form method="dialog">` submitted, page code calling `dialog.close()`)
  is bookkept as `'close'`. At close, focus goes to what `returnFocus()` gives
  **then** (on the page, enabled, not a hidden input, not inside `[hidden]`
  or `.d-none`), and
  `ux-dialog-close` fires with `{reason}`. It acts only on dialogs it opened:
  Generate and the English chooser keep their own handlers.
- `.ux-dialog-scroll` — a dialog whose body scrolls while its head and foot
  stay in view (`> form` a flex column, `.ux-dialog-body` the scroller).
  `.ux-dialog-foot-note` is the foot's reason line, before its buttons.
- `.ux-choice-list` — rows of `label.ux-choice`, one native radio each, with
  `aria-busy` on the list alone while it reads. With `data-ux-submit`, a
  delegated key handler adds Home, End and PageUp/PageDown (10 rows, held at
  the ends) over the radio group's rows not hidden, on top of the native
  arrows, and Enter submits the form once, only while its submit button (the
  first that is not `[data-dialog-close]`) is on, a disabled fieldset around
  it counting as off. Forced colours give each row a border, the chosen row a
  thick Highlight one and the row with the keyboard an outline.
  `.ux-choice[data-tone]` (`critical`, `warning`, `info`, `ok`) adds a
  start-edge bar (CanvasText in forced colours) beside the verdict in words;
  `.ux-choice-static` is a row that cannot be chosen, its reason written in it
  (no disabled radios); `.ux-choice-title` is a flex line (icon, code, words);
  `.ux-choice-status` is a row's small status line.
- `.ux-rows` — a list edited as rows (the slot dialog, §7.2 item 6, 6f): a
  table, one row per item, a row header (its number), text fields, then the
  row's own icon buttons (`.ux-rows-actions`, each named by its row; one that
  cannot act now is `aria-disabled`, so the keyboard stays on it, and drawn
  faded). `.ux-rows-time` is a 24-hour time typed as text (76px, left to right
  in either language), `.ux-rows-length` a computed value, `.ux-rows-flags`
  the row's pills; `.ux-rows-grow` (the label's cell) takes the room the
  others leave, each of them at least a fixed width, so two tables of one
  dialog line up. Below 560px each row is two lines (its number and label,
  then the rest), the header row out of sight only.
- `.ux-field-error` — a field's error under it, named by the field's
  `aria-describedby`; in a dialog a field the server refused
  (`aria-invalid="true"`) takes a critical edge, inside and out (3:1).
  Instructor management's `.im-form-error`, the exam timetable's period
  repeater and
  the older `.field-error` are to move onto it and `.ux-rows` (F12).
- `.ux-dialog-wide` — an 800px dialog, for one that edits rows.
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
- Dialogs opened through `UXControls.dialog` resolve `returnFocus` at close:
  it is a function (from an origin key), never a node kept from the opening,
  because the page may redraw the opener while the dialog is open.
- While such a dialog is open, what its work came to is said inside it (its
  `role=alert` line), never as a toast: a toast sits behind a modal, unseen.
  Work the user left with Escape that ends while another such dialog is open
  is said in that one; when that dialog is about something else (another
  meeting, even in the same dialog element: each records what it is about in
  `data-tw-about`), the words lead with what they are about (course and
  section, isolated), so they never read as that dialog's own.
  Its commit checks the page's edit hold itself before it writes
  (`dialogWrite`, which answers a failure as `{code, status, error}`, the
  last the server's own error), since the page's busy guard answers with a
  toast.
- The grid's columns are edited on PR 1's contract (§7.2 item 6, 6f): the
  page sends each list's rows as typed (label, start, end; never a flag, never
  an empty list, which the server would save as its defaults) and checks no
  time; the server's `VALIDATION_SLOTS` is marked on the row sent at its
  index, and its `SLOT_EDIT_NEEDS_CONFIRMATION` list is confirmed only by
  its fingerprint, after the user has read it. Unsaved edits are kept as the
  scenario's draft until a save lands.
- The filter and the programme lens are a view over the board
  (`applyGridView`, §7.2 item 6, 6c): they change attributes and text only —
  `data-dim`, `aria-describedby`, titles, a fold row's and rowgroup's
  `hidden` — and never the board's nodes, so a drag, a focused card and the
  keyboard's stop survive typing. Every meeting is drawn.
- Every meeting is drawn exactly once (§7.2 item 6, 6e): in the cell its key
  names — `cellKey`, one key for the renderer and the grid's rule, reading a
  time as the server does — or in the parking strip under its own table.
- Tests that pin a detail this design changes are updated in the same commit
  to pin the new contract; new tests are mutation-checked.
- The quality read (`GET …/quality/`) answers exactly as it always did.
  `?lens=1` (exactly `"1"`) adds `plan_lens` as its last key: the plan lens
  built from the quality read's own seating, equal to `/plan-lens/` for the
  same data (plus `programme_view`, below), or `null` (logged) when it cannot
  be built; a lens failure never costs the quality keys. Quality is never on
  the preview lane; `/plan-lens/` keeps the lane and its own seating. Both
  lenses end with `seating: {assessed, students[, reason]}`: `assessed: false`
  (reason `assessment_failed`) is unknown, never "nobody sits here".
- On `?lens=1` the lens ends with `programme_view` (6b): what each board
  programme's per-plan file shows, from the one rule the export draws its
  workbooks from (`timetable_programme_view.programme_placement_roles`).
  `{programmes, roles, shared_roles, placement_ids, seating_assessed}`:
  `programmes` are the export's (board programmes, board order, upper-cased);
  `roles[P]` and `shared_roles[P]` map `main`, `mixed`, `other` and `outside`
  (in that order, ids ascending, empty lists left out) to placement ids, the
  export's classification without and with SHARED, so a reader never
  intersects roles with the shared sections; `placement_ids` tells "not in
  P's file" from "not seen by this read"; `seating_assessed: false` means the
  roles follow the allocation and the marks. A view that cannot be built is
  `null` (logged), never at the cost of the lens or the quality keys.
  `/plan-lens/` does not carry it.
- The workspace (not split view) reads the plan lens only with the quality
  (`quality/?lens=1`, 6d), while it is wanted: the first quality read of an
  opened or reset scenario, while the filter holds a programme or SHARED, on
  Selection's Refresh and after a saved mark; every other read is plain. One
  quality read is in flight; a request joins it when it is about the same
  scenario and data and has the lens or needs none. The lens is never part of
  `TW.quality`. A programme in the filter dims by `programme_view`'s roles
  (any role, over the union; `shared_roles` with SHARED); the page holds no
  copy of the per-plan rule, and a meeting the roles never saw is unknown,
  never dimmed.

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

Pinned by `tests/test_timetable_workspace_grid_view_browser.py` (the
filter's dims and folds: contrast, the ring, the echo told apart, the
keyboard into a fold), `tests/test_timetable_workspace_parking_browser.py`
(the parking strip: under its table and across its columns, its header in
whole words at 12px, contrast, the teacher in a wide enough cell, every
table's days one to a row in the day column even with no slot column, M
moving a parked meeting onto the grid), `tests/test_timetable_workspace_frame_browser.py`
(frame, slot width, chrome height, type floor, contrast, blocked Publish
never posts),
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
   **As built, 5c (dialog layer, Change room, R):** the global parts came
   first, in their own commits: `UXControls.dialog`, the choice-list keys and
   `.ux-dialog-scroll`/`.ux-choice-list` (§3.3); `twFetch` handing every
   failure to `_onError` once (contract C); `TimetableRoomChoice`, split
   view's rules and the words, held to split view by a parity test; and the
   server's room candidates (#173). The page has one gate,
   `cardActionRefusal`, whose words are `REFUSAL`, for R and for Selection's
   Room button (Change…, or Choose… with no room yet; none for a section
   online or needing no room; drawn off with its reason under the fields,
   and kept in step with the gate in place); one seam, `TW.keyActions.room`
   (Move and Place join it in 5e and 5f); origins as focus keys, resolved at
   close; `dialogWrite`, `dialogSay` and `previewRead` as contracts C and E
   say (an answer is kept per meeting and data version until a board answer
   arrives; a failed read is not kept, so the next open asks again). The
   dialog reads the rooms at the meeting's own time and sends `{room}` alone,
   undo and redo too, so a time lock holds; the current room is chosen at
   first and holds Assign; a fitting room in use can be chosen (its row says
   who uses it, and the toast after the save says so again); misfits are
   rows with their reasons, in a group closed unless nothing fits; the
   filter hides rows in place. After a save the dialog closes, the board's
   tab stop moves to the card, the board is read again, then the toast.
   Where it differs from the spec, and why: the "also changes on N other
   boards" line is not shown before the save — the page holds only the open
   board's placements, so the count comes after it, in the toast, from the
   answer's `placements` (the spec's own fallback); the "this meeting only"
   words are the sub-line's, as the Strings table has them, not repeated in
   the body; a refusal of the save is said in a `role=alert` line
   (`#twRoomAlert`, page-local `.tw-dialog-alert`) between the scrolling
   body and the foot, which contract C needs and the template sketch lacked;
   after the save the board on show is read again, as a drop does (the same
   board unless the user switched boards while a save the dialog was closed
   on was out); the toast leaves out the instructor backstop, since a room
   changes no teacher; a class held on several boards counts once in "and N
   more"; a group with no rows is left out; a row's description also points
   at its occupants. Two mutants are equivalent, and recorded so: Assign
   without `data-tw-mutation` (the dialog's own sync turns it off whenever
   the page cannot write while it is open; dropping both is caught), and
   `dialogWrite`'s `_onError` answering false (the request is quiet, so no
   toast follows either way, and the code is taken before the answer).
   After review (round 1): a save the user left with Escape (D17a) ends
   where the user is: in the alert line of the helper dialog open by then
   (R again while the write was out, D3b; `dialogSay` looks for it), else as
   a toast, never as a toast behind a modal; the line takes the tone of what
   it says (`data-tone`: a refusal is a warning, a saved change a success),
   and a refusal of the same meeting holds Assign there too. Rooms read
   before the data moved are not kept on show: each read records the data
   version it asked at (`ROOM.version`), and a write that lands while the
   dialog is open, or a meeting that left the board (a scenario reload) and
   came back, makes them out of date. While no row has been chosen from
   them the dialog reads them again by itself: the list loads and comes back
   (the States table's "comes back on by itself"; nothing the user did is
   lost), the filter stays drawn while it loads so a keyboard in it stays
   there, a read still out is overtaken and its late answer never drawn,
   and nothing is read for a meeting off the board. Once a row has been
   chosen from them, the stale state holds Assign until Check again (which
   drops that choice), so a choice is never carried silently onto rooms
   whose verdicts may have changed; a meeting gone when Check again has read
   the board is read once it is back. Until the user chooses, the choice
   follows the current room as the board has it (after the escaped save,
   the saved room), and a keyboard on a row the dialog chose goes with it;
   a keyboard the user put on a row stays there. The "busy", "job" or
   "published" alert of a commit the page's own hold met goes when that
   hold ends or turns into another. Undo keeps the raw old room
   (`UNASSIGNED` included), now pinned. RC12–RC19 and RB3's note and alert
   contrast (in both tones) pin these and the reviewer's survivors; 59
   mutants run against the RC tests, all red but one, equivalent: a late
   answer drawn into a closed dialog (the next open starts a new session
   and draws everything again; nothing reads the closed dialog). Of the
   others the reviewer called equivalent, the open guard (the seam 5e and 5f
   call), Selection's sync after its draw (a gate that moved under the same
   markup) and a current room that does not fit (never chosen; the note asks
   for a room) are pinned now.
   After review (round 2): `#twRoomDialog` is one element whichever meeting
   it is open on, so a save left with Escape could be said, bare, in the
   dialog of another meeting opened meanwhile ("Room R101 assigned." under
   "Change room · AI333 S1"; a refusal read as that section's). Each helper
   dialog now records what it is about (`data-tw-about`, set when the room
   dialog opens), and `dialogSay` leads words about anything else with the
   meeting's course and section, isolated, in both languages (contract C,
   and the Strings row); a toast stays as any save's, and a refusal still
   holds only its own meeting's Assign. Move and Place (5e, 5f) get the same
   by recording their meeting. A choice the filter hides now also drops the
   pick, so nothing is chosen, by the user or the dialog: rooms read before
   the data moved are then read again by themselves rather than held for
   Check again. That exposed a gap the auto read shared with any redraw of
   the same markup: the rows kept a check the filter had dropped while the
   dialog chose the current room again, so the note said "This is the
   current room." with no row checked; every draw now gives the rows the
   choice, and the filter drops it again if it hides it. RC12 (the other
   meeting's refusal and save, en and ar, and a dialog about the same
   meeting keeping the words as they are) and RC13 pin these; of 13 new
   mutants two are equivalent and recorded so: words named while clearing
   (no clear passes `about`), and rows given the choice but never unchecked
   by it (a row the user checked is the choice, picked, so the markup moves
   with it and the rows are drawn anew).
   After live QA (on a copy of the real data): the dialog offered 172FB007
   for CS163 S1 under "Free and fits", worded "For context: outside this
   board's programme rooms"; after Assign, Issues listed "Room unsuitable ·
   CS163 · 172FB007" as a warning and the card rang. That is Issues' rule,
   not a slip: the quality check's `incompatible` (from the rooming audit's
   `original_unsuitable_demand_ids`) holds a saved room to the meeting's
   compatible rooms, built in `timetable_exact_rooming` as type ∩ gender ∩
   programme pool (H14) for every board of the meeting, so a room with
   `department_fit === false` is a "Room unsuitable" warning once saved. An
   info verdict promised no ring (§7.1: info rows never ring a card), which
   the save then broke; the dialog must say what Issues will say (D24's
   spirit). Such a room is now its own group, "Free, but outside this
   board's programme rooms (n)", between free and in use, with its hint
   (choosing one saves a "Room unsuitable" warning); its verdict is Issues'
   warning ("Lowers quality: Room unsuitable · outside this board's programme
   rooms", the kind word pinned to `ISSUE_KINDS.roomUnsuitable`); a room in
   use that is also outside keeps its clash's group and tone, and its words
   add the pool, so nothing Issues will list is left unsaid; the current
   room, the filter, Assign's hold and the busy lists treat the group as
   the others. The report after the save is a warning that names it ("Room
   {c} assigned; Issues will show it as unsuitable for this board's
   programmes.", or, after an in-use report, "Issues will also show it as
   unsuitable…"). No verdict is info any more. The case table's R7 and R12
   changed and R30 (held here and outside) and R31 (a misfit outside, still
   a misfit) joined it; RC2, RC6, RC15, RC20, RC21 and, in Chrome, RB4
   (no "Room unsuitable" on the card before; the
   row, its verdict and hint; the warning report; Issues' "Room unsuitable ·
   AI111 · R105" row and the card's warning ring; English and Arabic) pin
   it. 18 jsdom mutants (the pool rule before the in-use rule, which hides
   the clash; an info tone; the group merged into free; the in-use pool
   words dropped; a success report; the report's words; the group not
   drawn; the current room, the count and the hint skipping it; a missing
   flag read as outside; outside before misfit; the kind word, lead and
   reason; the Arabic heading) and 2 in Chrome (the old verdict; a success
   report) all go red.
   What still differs, a server follow-up: room candidates count as fitting
   some rooms the audit does not. A room with no department fits any board
   in `_room_department_fits`, while the audit needs a programme in common;
   a room with no gender fits either gender there, while the audit needs the
   meeting's own; seats are judged against the section's registered or
   available capacity, while the audit ("Room too small") uses its buffered
   demand; and `department_fit` is this board's pool only, while the audit
   asks it of every board a shared meeting is on. Until the server reports
   fit as the audit does, the dialog may under-report those: a room it calls
   "Free and fits" can still be listed as unsuitable or too small once
   saved.
   **As built, 5d (one slot verdict, server):** both previews take
   `include_verdict` (`verdict=1`) and the move preview `offer_manual`
   (`offer=manual`, through `offered_move_columns` next to
   `effective_lecture_slots`). Without them the JSON is byte-identical, key
   order included, to an oracle recorded from `a5d8f96` before the change
   (`tests/fixtures/tw_slot_verdict_legacy.json`, with each request's query
   count, which every teacher mode keeps). The verdict adds no query, except
   three with the links on and one teacher per section off (see Teachers).
   Settled where the spec was silent:
   `source` names the check (the legacy evidence kind of the same check,
   else `cross_board_instructor`, `blocked_slot` or `h15_effect`), not a
   legacy evidence row: an item is judged by the verdict's own rules;
   `students` counts the distinct students the items name (an effect's are
   always among them); a one-meeting planned candidate's items have
   `meeting: null`; `verdict_rank` ranks the candidates returned (Place asks
   for all of them, D15); an English (policy) section's move preview is the
   planned preview, so its verdict has no effects, there is no
   `current_verdict`, and `offer_manual` does not apply. That is a place
   verdict: the section's move also leaves its old pattern, which can turn
   untouched overlaps critical, so it must not be shown for a policy move
   until it has the effects (5e sends policy sections to the pattern
   chooser; the function's docstring says so). Beyond the spec: the moving
   section's own copy at the candidate's very slot (same day, start and end)
   on another board is the same class (5c's rule, now `_same_class`, shared
   with room candidates), so the verdict never reports it as a room or
   teacher clash (the legacy evidence still does).
   **Teachers (review rounds 1 and 2):** the verdict's `instructor` and
   `instructorElsewhere` items judge who teaches a section by the publish
   check's keys, `_publish_teacher_keys`, extracted from
   `detect_board_conflicts`, which uses it too, over the map the publish
   check reads (`_VerdictTeachers`): the one-teacher map's teacher when the
   map has the section (one to be decided charges nobody, its row's name
   never counting), else its linked teachers (one teacher per section off),
   else its meeting row's name; a teacher id never matches a name. With one
   teacher per section (production) the map is the `pr4_map` of the
   assignment the preview already loads, built only for a verdict: no
   query. With the links off there is no map and the rows' names decide.
   With the links on and one teacher per section off (the local default,
   where the department works; owner decision, review round 2) the verdict
   reads every teacher linked to a section's course with
   `linked_section_instructor_ids`, the code `build_section_instructor_ids`
   runs in that mode: three queries (programme requirements; course links,
   with their teachers' names from the join the active-teacher filter
   already makes; sections), once per request and only with `verdict=1`.
   Its resolver charges a planned section not created yet as the publish
   check will once it is. V10 pins the cost: without `verdict=1` every mode
   runs the oracle's query count; with it, the oracle's with the links off
   and in one-teacher mode, and exactly three more with the links on and one
   teacher per section off. The legacy fields keep the legacy test
   (`_section_teacher_matcher`, which also matches a charged teacher's name
   with a row's name), so the oracle still holds. In
   one-teacher mode the verdict judges with the map in force now: a section
   the map leaves to its layout-aware closure can be given another teacher
   once the edit is saved, and a planned section is judged with the teacher
   it has before it is placed (creating it may name one,
   `name_new_section`). Shared rather than copied: H15's "0 if a clash-free
   pair exists, else shared" is `_h15_blocked_students` and who teaches is
   `_publish_teacher_keys` (`detect_board_conflicts` uses both), the
   section masks are built by `_section_masks_by_course` (behind
   `_placed_section_masks`), and both previews take the cross-board student
   rule (a warning at the threshold, info below) from
   `_cross_board_student_item` and their teachers from `_VerdictTeachers`.
   Known divergence on the publish side (a follow-up):
   `detect_cross_board_conflicts` counts every same-course pair across
   boards that overlaps in time, a shared section's own copies and
   different sections of one course alike, each with "shared" = the
   course's whole demand (the course's students met with themselves), so
   the publish check can warn (`cross_board_conflicts`) where the verdict,
   like the legacy preview, counts no students between sections of one
   course.
   **As built, 5c-audit (server, its own PR):** room candidates answer, on
   `?audit=1`, what the quality audit will say once each room is saved; the
   audit's room rule moved into one module both use (§7.3, 5c-audit). No
   client change yet.
   **As built, 5c-audit (client, `feat/workspace-room-audit-client`):**
   Change room reads `/room-candidates/?audit=1` (the `previewRead` key is
   unchanged) and, before a save, says the room rows Issues will show after
   it. This closes the gap the 5c note above left to a server follow-up
   (seats, rooms with no department or gender, every board of a shared
   meeting).
   - **When.** An answer is judged by the audit when `target.audit.assessed`
     is exactly `true` and the quality check has not said it cannot judge the
     scenario's rooms (`TW.quality.rooms.assessed !== false`: a fault in
     another meeting that the answer cannot see). `roomJudgement` decides once,
     when the answer arrives (`ROOM.target`, `ROOM.unassessed`): a quality
     check answering later never re-sorts a list a room was chosen from (the
     next open judges again), and a save is reported by what its row was
     judged against, also a save the user left with Escape while the list
     open by then is judged otherwise. Otherwise the legacy rule holds,
     byte for byte: an answer with no `audit` key (a server before
     5c-audit) draws and saves exactly as before (RC22 replays a snapshot
     recorded at `5892a12` before any client change); one that asked and is
     not judged by it draws the same plus the note "The quality check cannot
     judge rooms for this meeting; rooms are grouped by type, gender and
     seats only."
   - **The rule** (`TimetableRoomChoice` v2): `REFUSED_AUDIT_REASONS =
     ['gender', 'type']` (the owner's answer to Q1: a room short of the
     audit's seats is a choosable warning); `auditOf`, `refusals`,
     `issueKinds`, `verdict(room, p, target, refused)`, `misfitReasons`
     (named from `target.audit`'s need), `factsHtml` (seats spare or short
     of `target.audit.required_capacity`), `currentVerdict` and
     `auditNotes`. `fits`, `occupied` and the legacy verdict are the same
     code. Judged by the audit a room is a misfit when the audit refuses it
     (held or not); else in use (critical when this board holds it, else a
     warning), flagged (a free room the audit will flag: a warning) or free.
     Its words are Issues' card for it once saved: the tone's lead, then the
     kinds in `ISSUE_KIND_ORDER` (Room double-booked, Room too small, Room
     unsuitable, Room also booked on another board), joined with ", " as the
     card joins them in both languages; then why: "needs N seats" whenever
     "Room too small" is among them (also when only a copy that stays is too
     small; the page's `seatsNeeded` words), "outside this board's programme
     rooms" when this board is in `programme_board_ids`, else "outside the
     programme rooms of {boards}", "{board} has no programme", and "the same
     class stays in {room} ({boards})" for each room a kept copy holds when
     the class would be split. Codes and labels are isolated.
   - **The dialog.** Groups: free, "Free, but lowers quality (n)" with its
     hint, in use, then the misfits; the legacy "outside" group only for
     rooms judged by the legacy rule. Notes above the filter (and only when
     there are rooms): a copy staying in its room (by room, with its boards)
     and a board with no programme, both warnings; seats assumed for a
     course with no budget row (context; `capacity_source: "default"`); and
     the unassessed note (context). `.tw-room-note` takes `data-tone`
     (warning or info); the time's own clashes keep the critical mark. The
     current room's line adds what Issues say of it now
     (`target.audit.current`, its use at this time from its row, and what the
     meeting needs of a type or gender it misses on), nothing when Issues
     list nothing for it. A current room the audit flags is chosen at first;
     one it refuses is not. The report names the rows Issues will list
     ("Issues will show “Room too small”, “Room unsuitable”."), after the
     clash sentence when the room is in use ("Issues will also show …"), a
     warning; a success when Issues list nothing for it.
   Where it differs from the design (critique §5), and why:
   - `REFUSED_AUDIT_REASONS` is `['gender', 'type']`, not `['gender',
     'type', 'capacity']`: the owner's decision. The list is one constant
     (`verdict`, `refusals` and `misfitReasons` take another for the test of
     Q1's other answers).
   - "Issues shows" (singular) in the new hint and notes, as the dialog's
     hints already say it; the design's strings table wrote "show".
   - The report quotes the kinds (“…”, «…»), as the outside hint quotes
     “Room unsuitable”; the design's `{k}` was bare.
   - The judgement is fixed when the answer arrives, not at each draw (the
     design was silent): a redraw must not move a chosen row out of the
     choosable groups.
   - A room with no `audit` in an answer that has one is judged by the
     legacy rule (the server always sends one); `unroomed_copy`
     (`missing`) is not worded: Issues read a copy with no room from the
     board ("No room" on that copy), not as a row of this meeting.
   - The truncation line is unchanged: it counts the rows the server lists,
     which audit mode extends past 60 (`summary.total` is unchanged).
   Tests (spec changes said where they are): the case table gains 29 audit
   cases (a shared `audit_target`; `fits`/`occupied` stay split view's
   answer), and its test checks each by group, tone, words (en, ar), reasons
   and facts, with the shapes that matter pinned (refused for gender alone
   and type alone, held and refused, short of seats alone choosable, both
   ways the two rules part, Issues' kinds alone and together in Issues'
   order; and answers that asked but are judged by the legacy rule). The
   kinds' words and order and `needsSeats` are pinned to the page's
   `ISSUE_KINDS`, `ISSUE_KIND_ORDER` and `seatsNeeded` (the kinds test's key
   list gained `roomSmall`, a spec change); new module tests cover the
   refused list (C2), `issueKinds`, `currentVerdict`, `auditNotes` (with
   Arabic counts) and server strings put in as they are. Split view's
   parity file now pins that the two pages differ only on answers that carry
   the audit, both ways (C12), and that split never asks for it (C6). Page:
   RC1 expects `?audit=1` (C6, a spec change); RC22 (the legacy snapshot,
   C4); RC23 (groups, tones, words, facts, misfits, the current room, en and
   ar; C1, C7, C9); RC24 (reports, also of a save left with Escape; C8);
   RC25 (each combination of quality reasons and use: the row's words are
   Issues' card text and tone after the save, and "needs N seats" is Issues'
   row; C3); RC26 (the quality check that cannot judge, and an audit that
   did not judge the meeting: the snapshot plus the note, and the judgement
   fixed at read time; C5); RC27 (the notes, escaped and isolated, none
   without rooms; C11); RC28 (a refused current room, one in use, one that
   fits, none). Chrome (`RoomDialogTests`): RB2 now expects R104 (10 seats
   for the audit's 22) among the choosable rooms and R103 alone refused, and
   RB4 the flagged group, its hint and the new report (both spec changes:
   the audit rule); RB3 also checks the notes' tone marks in both themes;
   RB5 is new: a 25-seat room for the audit's 26 (the budget's 24, +10%) is
   the "Room too small" warning before the save, the report says so, and
   Issues list "Room too small · AI111 · R106 · needs 26 seats" and ring the
   card, in English and Arabic; the lab stays refused. Mutation: 80 jsdom
   mutants (the refused list each way, the kind order, the quality-kind map,
   `auditOf`'s three conditions, each verdict, detail, note, current-line
   and word rule, the page's request, judgement, groups, notes, current
   line, escaping and report, and the legacy paths) all go red; two
   survived the first run and were pinned (the current line's words
   unescaped, now RC27 with a label holding markup; the current line while
   the rooms load, now RC28 byte for byte). In Chrome, 5 mutants all go red:
   seats refused (RB5, RB2), the audit not asked and the report by the
   legacy rule (RB5), and either note's tone mark dropped (RB3).
   Assets: page `?v=55`, `timetable-room-choice.js` `?v=2`,
   `timetable-workspace.css` `?v=15`; `global.css` unchanged.
   Known limits: split view keeps the legacy rule (F3, next); a slot change
   is not this dialog's (it is room-only), so `save: "slot_change"` answers
   have no client yet; this PR has no live check on the real data.
   **As built, 5e (Move and M):** M on a card and Selection's Move… (before
   Lock; none for an English section, which has "Change section pattern")
   go through the seam after the one gate (published, loading, job,
   external, locked: the first wins), and an English section to its
   pattern chooser after "Finding patterns…". The dialog reads
   `/slot-candidates/?verdict=1&offer=manual` through `previewRead`, draws
   "Now" from `current_verdict` and the other times in `verdict_rank` order,
   worded and toned by the verdict (never the legacy tone), and chooses the
   best time that can be chosen. Its body is exactly `{day, start_time,
   end_time}`, sent by `commitPlacementMove`, which a drop now uses too: one
   body, one undo entry holding the board's raw times, the redo stack
   cleared. The report (`reportEdit`, contract D) runs once the board has
   been read again and names the issue model's fresh critical and warning
   rows for the moved meeting; a drop shares it, so a drop's toast is what
   the card's ring and Issues say, no longer the board-scoped "Moved with N
   conflict(s)".
   Where it differs from the spec, and why:
   - The open guard comes before the gate and before the English route
     (contract A: every opener returns early while a dialog is open; a
     refusal toast would sit behind it, and the chooser is a dialog). An
     English section on a busy page says "Wait for the current operation to
     finish." rather than nothing: its chooser saves as soon as a pattern is
     chosen, and `chooseEnglishWorkspacePattern` returned silently.
   - `#twMoveStatus` is a visible line above the scrolling body, not inside
     it: what the list is doing (finding times, a failure, no other time, the
     data moved, the meeting gone or refused since, moving) stays in view
     wherever the list is scrolled, and is said (the line is always in the
     page, empty and with no height when there is nothing to say, so every
     change is announced); the body holds the rows and the way on (Try
     again, Check again). "Moving…" is that line's; the foot
     note gives the first hold (published, a planner job, busy), else
     "Chosen: {verdict}".
   - The sub-line opens with "This meeting only:" and says the other meetings
     stay only when the section has other meetings (Change room's rule);
     otherwise it is the time, then the room clause (none for a section that
     needs no room).
   - The snapshot a commit checks is taken as the times arrive: its day,
     start and end are the answer's own `placement` (what the server computed
     them for: the board may not yet show a write that landed while the read
     was out), with the scenario, the board, the lock and another scenario's
     claim as the page has them. Any change stops the commit with "This
     section changed while the list was open. Nothing was moved." (M9 pins
     each field), and the list says "The timetable changed since these times
     were read." with Check again (new words: the data that moved need not be
     this meeting's).
   - Data that moves under an open dialog follows Change room as built: with
     no time chosen by the user the times are read again by themselves (never
     while the dialog's own move is out: that write lands just before the
     dialog closes, and must not cost a preview read); a time the user chose
     waits for Check again; a read the write overtook is never drawn; a
     meeting off the board (a scenario reload) is read again once back; a
     meeting the board shows locked or claimed since says so and reads
     nothing until it is free, Check again included.
   - NOT_FOUND holds Commit, as Change room's does, beside the lock, another
     scenario's section and published; a duplicate and a busy scenario do
     not. A failure that does not say whether the move happened ("The move
     was not saved.") reads the board, whose answer stops a second move if it
     did (M9).
   - After a move the board's tab stop goes to the card whatever the origin
     (contract B, as Change room), not only from a card: from Selection the
     keyboard goes back to Move…, and Tab into the board reaches the meeting
     where it now is.
   - The verdict line names context kinds only when the verdict holds nothing
     worse (no card ever rings for context); an H15 effect's line names its
     board when it is another (its pair can be on any board).
   - A report or refusal of a move the user left with Escape goes where
     `dialogSay` puts words (the helper dialog open by then, led by the
     meeting's course and section when that dialog is about another), the
     instructor backstop in the same line; with no dialog open both are toasts
     as before.
   - The chooser gives the board's stop the keyboard back (`boardNav.restore`)
     only when it was opened from the board and the keyboard is nowhere after
     it, so a list row's chooser stays the list's (M16, W10). After a pattern
     switch the stop goes to the new row of the meeting's day on the open
     board (the switch can answer with copies on other boards).
   - Busy while open is the direct call in `syncWorkspaceControls`, as for
     Change room, not a `tw:planner-busy` listener; the "not committing" it
     asked for is the skipped re-read above.
   M1–M16 (jsdom) and MB1–MB3 (Chrome, `MoveDialogTests`) pin these; RC6 now
   expects Selection's note to give Move…'s lock reason on a locked meeting.
   Of 105 jsdom mutants all go red but two, both equivalent: Retry without
   `fresh` (a failed read is never kept, so Retry asks again either way) and
   Move…'s held attributes dropped from its markup (Selection's sync puts
   them on right after every draw); the 7 Chrome mutants (the scroll shell,
   the isolated times, focus back to a redrawn Move… and a redrawn card, the
   legacy tone, a room sent, the lock check) all go red.
   **After review (5e):** the keyboard moves to the chosen time before
   Commit turns off; R's hint is added only beside a room problem the
   report itself names (the clash the verdict saw may be gone after the
   save). The report lists every fresh critical or warning row of the
   moved meeting (contract D), so it can name a row that was there before
   the move as well as the one the chosen time's verdict showed. A drop
   whose scenario changed while the write was out reports nothing (the
   page shows another scenario by then). Equivalent, not pinned:
   `ISSUE_KINDS.instructorElsewhere`'s tone (no code reads it: the board
   model never produces the kind, the dialog takes severity from the
   server) and Retry's `{fresh:true}` (a failed read is never kept).
   **As built, 5f (Place… and its list rows):** a placeable budget row is the
   control (`aria-haspopup="dialog"`; its head the code, the label in `<bdi>`
   and a "Place…" hint hidden from assistive technology, shown on hover and
   on the keyboard's focus, gone while the row cannot be dragged; "{n}
   meetings a week" in its facts when n > 1, `pluralAr` in Arabic); the
   English row's nested button is gone, and one click listener on the list
   answers the hint. Enter (or the hint) on a complete course says so, on an
   English row opens its chooser, on a row of the unplaced list says that
   keyboard placement needs a section budget (F4), and on any other row
   goes through `TW.keyActions.place(request, {origin})`. The keys line says
   "Enter: place the section (opens a dialog)" / «Enter: وضع الشعبة (تُفتح
   نافذة)». `#twPlaceDialog` has Move's shell. Its one read is
   `/planned-slot-candidates/?course_code&course_key&section_label&limit=1000&verdict=1`
   through `previewRead` (`planned|board|key|label`); the rows follow
   `verdict_rank`, a pattern the server lists twice is one row at its best
   rank, a row names every meeting by day (", " / "، ", a lab marked), and
   its verdict and at most three items are Move's (`moveLines` and
   `moveDetailHtml`, now taking the board and a lead), each item with a
   `meeting` index led by that meeting, in the server's own order of the
   candidate's meetings. Day and Time filter in place; a day and a time
   must be one meeting's; a chosen row the filter hides gives way to the
   first one it shows (the dialog's choice, not the user's). A drop of a
   course that meets more than once a week opens the dialog on the dropped
   time, nothing sent; so does the server's "complete pattern" refusal of a
   one-meeting drop, the budget read again. The body is
   `{board_id, course_code, course_key, course_name, section_label,
   capacity, meetings}` (`plannedCreateBody`), sent by
   `commitPlannedCreate`, which the one-meeting drop now uses too: one body
   (`meetings[]`, no `day`/`start_time`/`end_time` keys), one undo entry,
   `section_create` (the ids, the body sent, the meetings as the server
   stored them). Undo removes every meeting, the last made first, skipping
   a meeting a removal answered as removed with it; a refusal leaves the
   rest on the stack and the board is read again when some went (D23); Redo
   creates them all again and `remapHistory` moves every id on both stacks.
   On success the dialog closes (the keyboard back on the row, by its key),
   the board's tab stop goes to the first new card, the board is read
   again, the new cards pulse, and the report reads the issue model
   (contract D): "Placed AI222 S2. · Lowers quality: Room not assigned yet ·
   Press R on the card to change its room." (a new meeting has no room, a
   room row, so R's hint is beside it).
   Where it differs from the spec, and why:
   - The open guard comes first, then "Create a board first.", then the
     gate's parts that apply (published, loading, a planner job): 5e's
     order (a toast behind a modal is never seen). A planner job refuses
     the opening (the Guards line, D3a); the States row's "Job or write in
     flight: opens, Commit held" holds for a write in flight, and for a job
     that starts while the dialog is open.
   - What the list is doing is said in the visible status line above the
     body (5e's), and that includes the drop's fallback ("No pattern
     includes Monday 09:00–10:15; showing every pattern."), so it is in view
     and announced; there is no second note in the body. The line is empty
     once every section is placed (the alert says it once).
   - Words the Strings table lacks, in both languages: the one-meeting
     failure ("The times could not be ranked." / «تعذّر ترتيب الأوقات.»)
     and clean count (Arabic «{n} وقت بلا تعارض», in `pluralAr`'s forms);
     "The timetable changed since these patterns were read." / «تغيّر
     الجدول منذ قراءة هذه الأنماط.» (Move's for one meeting); a read the
     server answers "board not found" says the "board changed" words, with
     no Try again. An item's lead is the short day ("Wed 10:30–11:45:"), as
     the line's own times are.
   - PL4's "190 نمط": in CLDR's Arabic rule 190 is "many", as 11 is, so it
     reads «190 نمطاً»; the test pins 190 so and "other" with 100 («100
     نمط»).
   - A label is checked against the budget, which is read again after
     every write: whenever the dialog syncs and before a commit, a label
     placed meanwhile becomes the next free one ("Section S2 was placed
     meanwhile; this is now S3."), read again, nothing sent; none left
     holds Commit ("Every section of AI222 is placed."). The server's
     `PLACEMENT_DUPLICATE` does the same, after `loadBudget()`. The server
     refuses a duplicate only on the same board, day and start: a create of
     a label already placed at other times adds rows to that section (a
     server follow-up, below), so the page must not send one.
   - After a duplicate the pattern the user chose is kept when the new
     label's list still offers it (its verdict can differ: a section's
     teacher can), not reset as Check again resets: the user had already
     asked to place that pattern, and resetting would turn the next Enter
     into another one. The alert says the label changed.
   - A failure that does not say whether the section was made (no answer,
     or a 5xx) is "Nothing was placed." as the spec says, and reads the
     board, as Move's does; if it was made, the budget then names its label
     and the dialog moves on to the next, so a second create of the same
     label is never sent. An answer that never came is not undoable.
   - The commit checks the board the dialog opened on (scenario, board, its
     load) and the read's data version; the label is not part of that check,
     since a new label is always read again before anything can be chosen.
   - The course's last section: the row now at that place is the course's
     own placed line (5a keeps a complete course reachable), not the next
     course.
   - Online: the board payload has no flag for a course not yet placed, so
     the foot note reads it from another section of the course on this
     board (`is_online` is the course's, per board); until one is placed
     there, the rooms note shows.
   - The unplaced list's English rows keep their pointer button (they are
     not budget rows and have no hint). A create the user left with Escape
     and the server refused as a duplicate says "The board changed; nothing
     was placed." (no dialog is left to move on); "complete pattern" says
     "Nothing was placed.".
   - `dialogWrite` answers a failure's `status` too (Move ignores it).
   - D24 for drops: a one-meeting planned drop and a drop from the unplaced
     list report from the issue model ("Placed …"), no longer "Placed with
     N conflict(s)".
   The payload (D15, Risk 1), measured on a copy of the local data, for the
   next section of every course in its boards' terms on scenario 665: 60
   times for a course that meets once (176 KB median, 242 KB at most), 70 patterns
   for twice (283 KB, 343 KB) and 190 for three times a week (979 KB, 1.04
   MB; 27 KB gzipped), the server's work 37 to 45 ms median on this machine.
   The app sends JSON uncompressed (no GZip middleware), so a three-meeting
   course costs about 1 MB per read; the memo keeps it to one read per
   section and data version (a server follow-up, below).
   PL1–PL17 (jsdom) and PB1–PB3 (Chrome, `PlaceDialogTests`) pin these;
   J2, J3 and J6 pinned 5b's refusal of a multi-meeting drop and are gone
   (PL2 and PL5 pin what a drop does now); W5 expects no nested button.
   Of 162 jsdom mutants all go red but one, equivalent: the board's
   stop named with `{focus: true}` before the board is read again (the card
   is not drawn yet, so the key waits, and a waiting key never takes the
   keyboard). The 7 Chrome mutants (the list's Enter, Home and End, the
   keyboard sent to the board after a place, the hint on keyboard focus,
   the old POST of a drop, the whole dialog scrolling, the times painted
   right to left, `dir="ltr"` removed) all go red.
   Follow-ups (server): refuse a create of a label already placed in the
   scenario (today only the same board, day and start is a duplicate);
   name the courses online for each board in the board or budget payload;
   compress JSON answers (or leave the legacy fields out with `verdict=1`).
   **After review (5f):** a create the user left (Escape closes the dialog
   without a new session) counts as left for every refusal: "complete
   pattern" says "Nothing was placed." and a label placed meanwhile says
   "The board changed; nothing was placed.", where the user is (the dialog
   open by then, named, or a toast), and nothing is read or relabelled for
   the closed dialog (PL12). The title's course code is escaped (PL18).
   Equivalents, kept as belt and braces: the board's stop set before the
   board is read again (`highlightWorkspacePlacements` sets it to the first
   new card after the read; PL19 pins the outcome), the list row's label
   escape (labels are `S<n>` made on the page), the `ts:` check in
   `activateListRow`, and the label check in `commitPlace` (every budget
   sync relabels).
6. **What the grid shows.** The lens from one server function shared with the
   per-plan export (roles including out-of-plan), its cost measured first and
   one evaluation per data fingerprint shared with quality; filter and lens
   per §7.1; the parking strip; the slot dialog replaces the textarea on the
   server contract from PR 1.
   Specified in full in §7.4 (six sub-PRs, 6a–6f; the owner decided Q4 and
   Q5 there).
   **As built, 6a (one seating for quality and the lens, server):**
   `GET …/quality/?lens=1` (exactly `"1"`; `true`, `0`, `yes`, `1.0` and an
   empty value are ignored) answers quality plus `plan_lens`, last, built
   inside the same one-teacher scope from the quality read's own seating.
   Without the parameter both reads are what they were: the oracle
   `tests/fixtures/tw_quality_legacy.json`, recorded at `b376afa` before any
   change by `tests/record_tw_quality_legacy.py`, holds the full JSON of
   `/quality/` and `/plan-lens/` (ids named, key order kept) and each
   request's query count in three flag modes (the settings' defaults for the
   three production flags, production, and production with the legacy seat
   layout); SQ1 replays it. The only change to `/plan-lens/` is `seating`,
   its new last key. One derivation, `seating_evidence(states, grouping=)`,
   reads a seating's `assignment_states`; no path reads the section states a
   seating mutated. `_section_assignment_evidence` keeps its name (the
   export's seat-once pin is untouched) and returns a frozen
   `SeatingEvidence`. A seating that cannot be evaluated used to be a silent
   `{}` that read as "nobody sits here"; it is now logged once and reads
   `{assessed: false, students: 0, reason: "assessment_failed"}` on both
   endpoints (scenarios 662 and 664 on the QA copy). The hand-over: the
   evaluation's states when the seating ran; nobody to seat without demand
   (assessed, 0 students); otherwise, when the instructor step failed before
   the seating or the seating step raised (also after the seating, for
   instance while reading its score), the lens seats on its own (SQ8, SQ8b),
   so it always equals `/plan-lens/`. Any failure building the lens is
   `plan_lens: null`, logged, with the quality keys untouched (SQ9).
   Found on the way, and fixed: (1) holding the evaluation while the lens
   read its own inputs raised the read's Python peak by 1.08 MiB (the budget
   is +1 MiB), so the evaluation is dropped once its evidence is read; (2)
   the lens re-read the demand the quality read had just read (10 of its
   15 ms on 665), so the quality read now reads the demand rows itself, at the
   point the profile builder did, hands them to the builder (a new optional
   `demand_rows=`) and, only when the lens is asked, holds them for the lens
   (`demands=`); plain quality lets them go before the seating, as it always
   did. Weakref tests pin both lifetimes; SQ1 pins that plain quality's reads
   did not change.
   **Budget, 665** (QA copy, production flags, this machine, SQLite):
   `lens=1` adds 4 queries to quality's 44 (the seated students' programmes,
   the demand's programmes, budgets, sections; SQ3 pins them; with no demand,
   2), against 59 for quality plus `/plan-lens/`. Measured directly, the
   extra work is the derivation (2.2 ms) and the lens with its seating and
   demand handed over (3.6 ms; 14.3 ms when it read the demand itself), on a
   quality median of 69–79 ms. Interleaved medians (budgets ≤ 1.25× quality
   and ≤ 0.70× quality plus `/plan-lens/`): four runs of 63 rounds (fixed or
   rotated order, with or without garbage collected before each call) read
   1.09–1.14× and 0.58–0.64×, and 1.08–1.11× and 0.61–0.63× at the minimum;
   two runs of the spec's 21 rounds read 1.12–1.13× and 0.72×, just over the
   second line. The machine is shared with other builds: the separate reads'
   median moved by up to 30 ms between runs, while the minima did not. The
   same probe with memory measured (tracemalloc) before the timing read
   1.29–1.42×, an artefact of that order, which F's probe (`combined2.py`)
   shares, so F's +34 % may overstate the cost as well; the figures above
   were timed first. Python peak: quality 3.36–3.74 MiB, as at the base;
   `lens=1` 3.62–4.01 MiB (+0.3 MiB, the demand rows held for the lens; 663:
   1.19 and 1.50–1.64). JSON: 6.6 KB without the lens, 58.5 KB with it
   (5.3 KB gzipped). Real data: on all five QA scenarios (662–666), in both
   flag modes, quality's JSON and query counts and `/plan-lens/`'s (less
   `seating`) equal the base commit's, and `lens=1`'s lens equals
   `/plan-lens/` from one seating (none where the sections cannot be built).
   Not measured: the 0.5 vCPU host (F16).
   **Deviations from the spec:** `build_scenario_plan_lens` takes `seating=`
   and `demands=`; its `programme_view=` comes with 6b, which gives it a
   behaviour. SQ3's N is 4, not 5 (the demand is not read twice). SQ7 is in
   `tests/test_tw_preview_lane.py`: the quality view is listed as never laned
   (`UNLANED_READS`), and a full lane still answers `quality/?lens=1`.
   **Tests:** SQ1–SQ9 and SQ8b, the derivation (non-roster ids, a roster id
   with no student row, an id with no section, a course key holding `_`),
   the evaluation dropped and the demand rows held only for a lens, and the
   profile builder's `demand_rows=` (`tests/test_tw_scenario_seating.py`,
   22 cases). Of 41 mutants (each compiled first, so none goes red on a
   syntax error) all go red, among them: truthy `lens`, the lens laned, a
   second seating, grouping from the wrong run, the failure read as assessed
   or as nobody, a silent failure, the lens failure failing quality, the
   evaluation kept alive, the demand read twice or held by plain quality, the
   seating key out of place, and an extra query or a key moved in plain
   quality. Not mutated, by construction: the hand-over carries only the
   evaluation and the demand rows, so the combined path cannot read the
   section states the seating mutated; and the lens is built inside the
   one-teacher scope, which memoises teacher reads the lens never makes.
   **After review (approved; its non-blocking items fixed):** (1) SQ4b: when
   the quality read's own demand read raises, the read still answers 200, the
   three seat parts read `assessment_failed` (logged once), and `lens=1`'s
   lens seats on its own and equals `/plan-lens/`. It kills the demand read
   moved out of its `try`, which would make the read a 500. (2) SQ5b: with no
   demand and no `lens`, both reads equal the base commit's, query counts
   included. The oracle gained a fourth entry, `production_no_demand`,
   recorded by the same recorder from `b376afa`'s code (a worktree at
   `f518662`); the three earlier entries came out byte-identical. It kills
   `if demand_rows is None` → `if not demand_rows` in the profile builder
   (one more query). (3) SQ2b: with production flags and nothing marked, the
   lens from quality's seating and `/plan-lens/` both say
   `programme_grouping: null`; it kills a non-empty default for the
   evaluation's grouping. (4) SQ6 also ignores `lens=%201`, a padded `1`.
   (5) The failure path is not cheap, unlike what the docstring said and
   §7.4's "fails cheaply" says. When quality's seating never ran, the lens seats on
   its own, and that seating reads the demand and builds every student's
   profile before the sections fail; the lens then reads the demand again.
   Measured on the QA copy (production flags, interleaved, 63 rounds,
   medians): on 662, plain quality 17.7 ms and `lens=1` 44.1 ms (2.49×). The
   extra 26 ms is mostly the profiles with their demand read (12.9 ms), the failing
   section build (3.3 ms) and the lens's own demand read (7.6 ms). That
   equals quality plus `/plan-lens/` (0.99×). On 664: 22.7 against 59.5 ms
   (2.62×, 1.00×). Python peak: quality 0.65 MiB, `lens=1` 3.62 MiB,
   `/plan-lens/` alone 3.53 MiB (664: 0.83, 5.05 and 4.95). Queries: 20 + 10
   = 30, with the demand read twice. So on such a scenario `lens=1` costs what
   the two reads cost apart: no more, and no saving. The docstring now says
   so. Building the sections before the profiles would make the failure
   cheap; the triage kept the code and fixed the wording.
   **Tests:** 26 cases; the six mutants of these fixes (the four above, the
   seat failure left unlogged, and `remove_demand` removing nothing) all go
   red on the intended test. Follow-ups: none.
   **As built, 6b (programme roles shared with the export, server):**
   `core/services/timetable_programme_view.py` is the one rule for "which
   placements P's per-plan file shows, and as what". The export's code moved
   there verbatim (each moved definition compared by its source against the
   base): `MAIN_SECTION_SHARE`, the role names, `SectionSeat`,
   `_lens_seating`, `classify_course_sections`, `_section_allocated_to_plan`,
   `get_plan_term_map`, `get_plan_elective_terms`, `_classify_placement_term`,
   `_placement_plan_term`, `_norm`, `_upper` and the course-key helpers. The
   only edits: `_section_passes_search` and `_section_matches_plan_filter`
   take `shared_only: bool` instead of the export's filter, and
   `get_scenario_programs` reads its boards through `_scenario_boards` and
   `_board_programmes` (the same query), so the view reads the boards once
   for programmes and terms. `scenario_course_keys` and `programme_context`
   are the export's own lines (the course keys; the plan map with this
   term's electives) as functions. New: `ROLE_OUTSIDE`,
   `programme_placement_roles` (the export's order of work: SHARED narrows
   each plan-term sheet's sections, each course is classified per sheet, and
   an out-of-plan placement is `outside` when it passes SHARED and the
   allocation) and `programme_view(scenario, plan_lens)` (contract B). The
   module imports the models and two small helpers; at call time
   `get_plan_elective_terms` imports the elective resolver (deferred, as it
   was in the export), which loads eight more modules (reporting, eligibility
   and the timetable snapshots among them). Neither loads OR-Tools, the export
   or the lens (a subprocess test checks the module's import; the deferred
   one was checked by hand). The export re-imports every moved
   name (explicit re-export aliases for the ones it no longer calls), draws
   each file from `programme_placement_roles(...,
   shared_only=export_filter.shared_only)` (`is_other` is role `other`; the
   text checks are unchanged), and its planned-section count, which held an
   inline copy of the SHARED test, calls `_section_passes_search`.
   `build_scenario_plan_lens(..., programme_view=True)`, which only
   `quality/?lens=1` passes, ends the lens with `programme_view`; one that
   cannot be built is `null`, logged once, the lens and quality untouched.
   **Workbooks unchanged.** The oracle
   `tests/fixtures/per_plan_export_digests.json` was recorded at `cd15266`
   before any change (committed in `ab49be1`, by
   `tests/record_per_plan_export_digests.py`, lxml blocked; recording it with
   lxml gives the same file): 29 scenarios (every scenario the grouping and
   electives tests build, the slot tests' off-grid one, and two that make
   SHARED, out-of-plan sections and an elective on two slot terms count) ×
   5 searches ("", `SHARED`, `AI2`, `AI2 CS1`, `SHARED DS`), each sheet a
   digest of its names, merges, widths and each cell's value, fill and font
   read back through openpyxl (91 term sheets). `TW_PER_PLAN_DUMP` writes the
   full lines and the support module names the first differing line.
   **Real data** (QA copy, local and production flags, all five scenarios,
   six to eight searches each, among them every board programme, `SHARED P`
   and `P CS1`): every workbook's sheet digests equal the base's, and for
   every board programme, with and without SHARED, the view's roles equal the
   keep set and roles the base export's own loop produced (181 to 468 role
   entries a scenario). Under production flags the export of 662 and 664
   fails before and after alike (F17).
   **Budget, 665** (QA copy, production flags, this machine, SQLite; timed
   before memory): `lens=1` at 1.24× quality's median (21 rounds: 84.5 against
   68.1 ms; 63 rotated rounds: 1.24×; minima 1.23-1.25×; budget 1.45×) and
   0.71× the separate reads (minima 0.70-0.71×; budget 0.80×). 6a's `lens=1`
   in the same runs read 1.09× and 0.62-0.63×: the view itself is 9.6 ms
   (median of 31 calls alone), mostly ORM rows (each programme's plan rows,
   the placements) and the 8 role passes. Queries: 61 = quality's 44 + the
   lens's 4 + the view's 5 + 2 × 4 programmes. JSON: `programme_view` is
   5,670 B as `JsonResponse` writes it (4,975 B compact; 666: 5,612 B), about
   6 % under the 6 KB budget. It grows with placements × programmes × 2 (each
   programme's roles and shared roles), so a scenario larger than 665 can pass
   6 KB (F16 measures one); the lens with it 58.8 KB. Python peak: `lens=1` 3.62-3.66 MiB against quality's
   3.35-3.36 MiB, as at 6a.
   **Deviations from the spec:** (1) `programme_placement_roles` returns
   `ProgrammeRoles(roles, seats)`, not only the roles: the export draws its
   blocks and cell notes ("AI2: 2 of 30") from the seats, and roles alone
   would make it classify every sheet twice. (2) PV7's count is 5 + 2 per
   programme, not 4 + 2: the fifth read is the scenario row, whose year and
   term the elective mappings need (the quality view only checks that the
   scenario exists, and the lens is handed its id), read by primary key in
   `timetable_plan_lens._programme_view`. It costs one indexed query; reading
   the row where the quality view checks that it exists, and handing its year
   and term down, would bring the count back to the spec's (F18). A
   programme whose term has mapped
   electives reads its plan rows once more (the export's own
   `get_plan_elective_terms`). (3) The
   export keeps no `_section_passes_search` adapter: after the move nothing in
   it passes its filter object, and its planned-section count calls the moved
   function with `export_filter.shared_only`. (4) Moved beyond the spec's
   list, because the view needs them without importing the export:
   `get_scenario_programs`, `_section_matches_plan_filter`, `_norm`, `_upper`.
   (5) A board programme spelt two ways ("ds" and "DS"; the board form
   upper-cases, so only data written elsewhere can) is one programme in the
   view, read as the first board spelt it, which is the first of the export's
   two files (PV5 pins it).
   **Tests** (`tests/test_tw_programme_view.py`, 55 cases): PV1 (the oracle,
   per scenario; its comparison names the first difference), PV2 (each role
   from the seating: the mark, 70 % and 69 %, mixed, other, a section
   allocated to AI2 that seats none of AI2 beside one that does, `outside`
   allocated, unallocated and with no lens row, another programme's
   out-of-plan section; the role lists' order), PV3 (one section on the T8
   and T10 sheets of an elective: mixed on one, other on the other), PV4
   (SHARED: the allocated shared section becomes main, a mixed/other pair
   changes; contract B's shape), PV5 (board order, upper-cased; a programme
   only the students or `scenario.programs` name is absent), PV6 (an
   unassessed seating: roles by allocation and, under the flag, the mark),
   PV7 (the reads for one, two and three programmes), PV8 (one definition;
   the export calls the roles function once per file with SHARED passed
   through), PV9 (on five scenarios, the roles each file was drawn from equal
   the view's), the view only on `?lens=1`, a failing view, and the import
   footprint. The 6a tests compare `?lens=1`'s lens less `programme_view`
   with `/plan-lens/` and pin the view's reads (SQ3, SQ5). Of 41 mutants
   (each compiled first) all go red, among them: SHARED applied after
   classification, classify everything then intersect, out-of-plan without
   the SHARED test, roles per course rather than per sheet, electives
   ignored, the seats not handed to the export, the union token set,
   lexical order, the last spelling, a query per placement, the boards read
   twice, unsorted ids, empty lists kept, the 0.70 boundary, allocation
   deciding in a seated course, the export ignoring SHARED or keeping a
   placement with no role, the planned count ignoring SHARED, the view on
   every lens or before `seating`, a silent or fatal view failure, and the
   oracle's comparison passing a difference. Against PV1 alone, 13 of 14
   export mutants go red; the 0.70 boundary needs an exact 70 % section,
   which PV2 and the grouping tests have. Equivalent, so not counted: a
   SHARED test added after classification while the one before it stays (the
   sheet already holds no non-shared section), a board term of `None` for
   0 (it changes a status, never a role), and `role is None` for
   `role != "outside"` on an out-of-plan placement (it has no other role).
   **Found on the way (F17, §7.4 follow-ups):** with one teacher per
   section on (production), the per-plan export of QA scenarios 662 and 664 raises
   `ValueError` ("English board copies must each use the same complete weekly
   pattern") from `section_teacher_texts` → `build_section_states_for_scenario`,
   before and after 6b: the download fails on a scenario whose English
   copies disagree. The quality read reports that scenario as unassessed and
   the lens as unassessed seating; the export has no such guard.
   **After review (approved; its non-blocking items fixed):** 6a's review
   fixes merged first (`1691bfb`); its new SQ2b and SQ4b now split
   `?lens=1`'s lens from its view, and their 6a mutants still go red.
   (1) Nothing checked that the view reads only its own scenario's
   placements: every view test's database held one scenario, so
   `SectionPlacement.objects.all()` passed. PV4's shape test now adds a
   second scenario with a placement (a course in no plan and with no lens
   row, so any programme that read it would list it outside); it is in
   neither `placement_ids` nor any role list, with or without SHARED.
   (2) R2's last tie-break, grid order (label, then id), came across
   verbatim and no test reached it. A PV2 case pins it: two CS112 sections
   seat 4 of 10 AI2 each, and S1, the larger id and placed second, is mixed;
   two ENGL214 sections with no label (both "?") fall to the smaller id,
   also placed second. (3) The note above now names the deferred elective
   resolver import, the JSON budget's headroom (about 6 % on 665) and what
   the fifth view read is for and costs. **Tests:** 56 cases. Eleven mutants,
   all red on the intended test: the view's placements, its ids alone or its
   roles alone read from every scenario; the grid order by id alone, by
   label alone, as placed or reversed, and the later section winning R2's
   tie; and 6a's three on SQ2b and SQ4b. **Follow-ups:** F18 and F19.
   Specified in six sub-PRs (6a–6f; the spec and its critique: E1–E24,
   contracts A–D). **Owner decisions (2026-10-23)** on the spec's two
   questions: Q4 — the slot dialog shows the online-only mark read-only and
   never sets or removes it (E15b); Q5 — a list always keeps one row, and
   there is no "Use the default columns" button (E15c).
   **As built, 6c (the text filter folds and dims, in place):** every meeting
   is drawn and typing never draws the board again. `applyGridView` (after a
   draw, a keystroke, a tone answer and a lens answer) writes attributes and
   text only: each card's `data-dim` (`filter`, `lens`, or both), its
   `aria-describedby` (`twDimNoteFilter`, `twDimNoteLens`: visually hidden
   notes in `.tw-centre`, outside `#twGrid`, whose words the page writes),
   its title (the tone's reason, its identity, then why it is dimmed); each
   table's fold; each group heading's "· N matches"; the legend's
   "Dimmed: outside the filter" line; and the count. A fold's tone counts are
   text nodes rewritten in place, so neither typing nor a tone answer adds or
   removes a node under `#twGrid` (FL2, FL3 watch for it), and the board's
   roving reads its stops again only when a table folds or opens: the page's
   `nav.refresh()` then, and the global `ux-disclosure` event a toggle (or a
   locate, `openFoldOf`) fires. A dimmed card is an ordinary card: it keeps
   its ring and marks, drags, and takes Enter, M and R (FL8).
   Each table's first row is its fold (contract D): one gridcell holding the
   global `.ux-disclosure-toggle` (no `data-ux-disclosure`, so no session
   key), drawn with `tabindex=-1` and a roving stop (`'.tw-card,
   .tw-fold-toggle'`), its cell spanning the table (`aria-colspan`); the
   header and day rows sit in a `display: contents` rowgroup it hides. Its
   label counts the cards drawn in the table ("Labs · no match · 0
   meetings"; an off-grid meeting is drawn from 6e). The open
   folds are page memory keyed `{group}|{kind}`, cleared when the text **or
   the board** changes — another board's "S1 lectures" is another table
   (an addition to the spec, FL4). The count stays "{n} of {total}", n the
   meetings neither filter nor lens dims; the field is described by
   `#twSearchNote` (its `aria-describedby`, was the count) and titled with
   the sentence "… tables folded for no match: {k}; programme filter: {P}",
   k the folds still shut (one the user opened is no longer folded), each
   clause left out when empty (6d gives the programme its role words). The
   Arabic sentence is "المطابق للتصفية: {n} من {meetings}" with the noun after
   من in the genitive plural (لقاء واحد، لقاءين، 3 لقاءات، 11 لقاءً): the spec's
   "{n} من {total} لقاء مطابق" left the noun singular after every number.
   The group heading's Arabic course count takes `pluralAr` too (it read
   "1 مقررات").
   The interim lens (until 6d): a programme the held lens knows and SHARED
   dim (`data-dim="lens"`) and never fold, through today's allocation rule
   (`itemMatchesPlanToken`), in contract D's shape: a union over the
   programmes, SHARED narrowing it (alone: every shared section). Today's
   search took several tokens as an intersection ("AI2 DS" kept only
   sections allocated to both). Its note: "Outside the programme filter:
   {P}, …" (isolated). A lens answer with a programme token in the filter
   dims anew in place (`reapplyPlanSearch` no longer redraws); names for
   duplicated codes still come from the lens read on open (E19 is 6d's).
   The text matches what cards and Selection show, never the lens's words
   (`placementText`: "UNALLOCATED" or "core" finds nothing, FL6). The left
   list keeps its own finder (E12); split view's chips, repair's plan scope
   and the export's free-text search keep their rules (E22, E23; F8–F10).
   Dark mode needed two rules beyond the spec's CSS: `html[data-theme=dark]
   .tw-card` and its `.cc` set the ink and outrank `.tw-card[data-dim]`, so
   the dark dim rule sets the ink and the code's colour too. Forced colours:
   a dotted GrayText border; a toned dimmed card keeps the 2px CanvasText
   weight, dotted. Global: the disclosure listener fires `ux-disclosure`
   (`{open, key}`, key null without one) and `UXControls.roving` refreshes on
   it (§3.3); `ux-controls.js` ?v=6, PAGE ?v=57, TWCSS ?v=17 (`global.css`
   unchanged). timetable-copy's harness now loads shared-utils, as base.html
   does (its Arabic draw needed `pluralAr`).
   Tests: D1 (accessibility); FL1–FL10 (workspace); W1 and the three filter
   tests (rooms in words, the teacher, a programme search read anew) now pin
   the fold row and `data-dim`, not absence (a spec change), and the keyboard
   browser test's empty-cell probe picks a `.tw-cell` (a table's first
   gridcell is now its fold, hidden while unfiltered); Chrome FB1 and
   FB2 (`tests/test_timetable_workspace_grid_view_browser.py`).
   Mutation-checked: 44 jsdom mutants (5 global, 39 page) and 8 Chrome
   mutants all go red (escaping the page-made course count in the group
   heading is belt and braces: no test can tell); one
   Chrome mutant first survived — a fold label in `--t4` at 60 % opacity:
   `--t4` passes 4.5:1 and the shared contrast helper reads colours, not
   opacity — and was replaced by a failing colour, which goes red.
   Measured (a copy of the QA database, scenario 665, its largest board: 44
   cards in 14 tables; Chrome, the time forced through style and layout, 24
   keystrokes): a keystroke (`applyGridView` and the left list) takes a
   median 2.0 ms (max 3.5); at 4× CPU throttling, about the 0.5 vCPU host,
   10.0 ms (max 16.7). Drawing the whole board again — what an edit does,
   and what the old filter did on every keystroke over the cards it kept —
   takes 16.2 ms, and 95 ms throttled.
   **After review (6c):** a tone update builds the issue model once:
   `applyCardTones` hands its `byPlacement` to `applyGridView`, which builds
   the model itself only when no caller gives it (FL3 counts the builds).
   The warning ink (`#9a3b06`; dark mode the shared `--sev-warning`) is one
   custom property, `--tw-warning-ink` on `.tw-grid`, which the cards, the
   folds and the legend all read (each set its own copy); Chrome computes the
   same ring, mark and icon colours before and after. Four reviewer mutants
   had survived; each test below now fails on its mutant. FL4: when a user
   has opened a fold and new text still matches nothing in that table, its
   rows hide while its fold row stays, and the board's stop leaves the
   hidden rows. Only the `moved` flag on the rows does this, because the
   fold row does not change. FL10: under a lens dim alone, a group heading
   does not count the card as a match, and the legend's dim line shows.
   FL10 also checks that adding or removing a programme token keeps an
   opened fold open, since the memory is keyed on the board and the text
   only. FB1: the fold label's effective opacity (its own times its boxes')
   must be 1. The contrast helper cannot see opacity.
   Follow-ups (recorded, not done): the fold label spans its table and
   starts at the inline start. If the board scrolls sideways (a wide slot set
   at a narrow width), the label scrolls out of view while the day column
   stays sticky; it could be sticky at `inset-inline-start`. The legend's
   "Dimmed: outside the filter" also covers a programme-lens dim, which it
   fits only loosely; 6d decides its words along with the lens's roles.
   **As built, 6e (the parking strip):** a meeting no column of its kind has
   is drawn, under its own table, and was drawn nowhere before. One cell key
   for the renderer and the grid's rule (E14): `cellKey(day, start)` is the
   day upper-cased to three letters and the start read as the server reads a
   stored time (`_TIME`, `_hhmm`: H:MM or HH:MM, seconds allowed, hours
   0–23), null for anything else, so "9:00", "09:00:00" and "sun" are drawn
   where the server says they are (the renderer compared raw strings, the
   rule `slice(0, 5)`). The lab rule is one function for both too
   (`isLabMeeting`, as `is_lab_meeting`: over 80 minutes, never English; it
   was a copy inside the renderer). A table draws each meeting in the first
   column whose key is its own, and every other one of its meetings — no
   teaching day, or no column of its kind at its start — in its strip, so
   every meeting is drawn exactly once: two lecture columns with one start
   (the server keeps both when their ends differ) used to draw a meeting
   there twice, with one focus key (PK5b). A lab table is drawn when there
   are lab columns or a lab to park, laying out at least one column. The
   English table's columns come from its meetings on the grid, so a weekend
   English meeting adds none and is parked under English.
   The markup is the spec's: the strip is the table's last row inside the
   rowgroup its fold hides (a fold counts its parked meetings, and a locate
   opens it); "Off grid" in the day column with the rest of its name
   visually hidden; one gridcell per meeting, a `tw-cell` container,
   `data-ux-col` its start in minutes and `data-focus-key`
   `park:{group}:{kind}|{id}`. The order is day (SUN…SAT, then any other
   value by its text), start (a start that is no time last, its column
   1440), course, then id (one course twice at one time). The keyboard needs nothing new from roving:
   ↓ from the last day lands on the parked card nearest in time, ←/→ walk the
   strip, ↑ goes back to the nearest column, PageDown to the next table
   (PK4). No drag listener is on the strip; a parked card drags onto a cell
   as any card does (PK4c), and M opens Move with the manual offer (Chrome).
   Three departures from the spec's text, each for a rule of the design
   record: the when-line is `{day} <bdi dir="ltr">{t}</bdi>`, the day in the
   line's own direction and only the time left to right, as Selection,
   Issues and Move write a meeting's time (the spec's `<bdi dir="ltr">{day}
   {t}</bdi>` paints an Arabic day to the left of its time, the one place
   on the page that would); it is `--text-xs`, the design system's floor for
   data (global.css), not `--text-2xs`; and the strip's header has a 4px
   inline inset, not the day labels' 8px, because "الشبكة" in the wide font
   was cut in two inside the 56px column (Chrome found it; its test now
   fails a word broken across lines). A parked card does not repeat its day
   and time in the card's visually hidden words (its line says them).
   Days: SUN–THU as the grid's words, Friday and Saturday named (Fri/Sat,
   الجمعة/السبت, Friday/Saturday where English uses full names), any other
   value escaped and isolated — one helper (`dayWord`, `dayText`, `dayHtml`)
   wherever a parked meeting's day is shown: its card, Issues, the locate's
   words, Selection's Day and meetings, and Move's lines. For SUN–THU every
   output is unchanged: an oracle of the grid, Issues and Selection (en and
   ar, a board with stacked, locked, lab, online and English meetings) and
   of the slot module, recorded before the edits, is byte-identical after.
   Issues: `offGrid` locates (its `locate: false` and the renderer's check
   for it are gone) and its detail ends "· under its table" / "· أسفل
   جدوله"; the card keeps its warning. The legend has one line
   (`#twLegendPark`, a swatch of the strip's tint) while any strip is drawn.
   `timetable-slot-config.js`: `confirmationHtml(details, {parked})`, the
   workspace passing true; without it the output is the one recorded before
   6e, to the byte (PK6), and split view's confirmation keeps its words.
   Versions: PAGE ?v=58, TWCSS ?v=18, the slot module ?v=4 in its three
   templates; `global.css` unchanged.
   Tests: the issue-row test now locates the parked meeting (a spec change);
   PK1–PK5 and PK7, with PK4b (order), PK4c (a drop) and PK5b (one start, two
   columns), in `timetable-workspace.test.cjs`; PK6 in
   `timetable-slot-config.test.cjs`, with the workspace's and split view's
   confirmations pinned; `grid_rule_cases.json` gains "9:00", "09:00:00",
   "sun", a weekend English meeting and "9am" (the server's rule passes them
   as it is); PK8 and the Chrome checks in
   `tests/test_timetable_workspace_parking_browser.py` (`ParkingTests`, an
   ORM meeting at Monday 18:30: 1366×650 and 1440×790, light and dark, en and
   ar — the strip under its table and across its columns, the header at
   12px in whole words and unclipped, contrast of the header, the card's
   code, room and day and time and the legend line, nothing faded, the
   frame's acceptance, the teacher shown in a cell at least 130px wide; and
   M → Monday 09:00 moves the card into its cell and the strip and its
   legend line go). Mutation-checked: 64 jsdom mutants and 12 Chrome
   mutants all go red. Three first survived and were strengthened: a parked
   card made undraggable is made draggable again by the page's own control
   sync (a mutant the sync cannot undo goes red); a faded header passed the
   contrast helper, which reads colours, not opacity; and a strip no longer
   spanning its columns still gave each card 140px. The Chrome check now
   measures the strip's span and every opacity. Not pinned, as decoration: the strip's tint and the legend
   swatch (no text sits on the tint), and `overflow-wrap: anywhere` on the
   header (both words now fit whole; it only keeps a longer word from
   clipping).
   Measured on a copy of the QA database: 788 placements in 5 scenarios,
   none parked by the server's rule, and the page's old rule and old
   renderer agreed with the new on every one (every stored time is HH:MM, on
   SUN–THU); no scenario has two columns with one start. Nothing on the QA
   data changes: the strip shows for what the old editors could save (Risk
   1: fixtures and an ORM meeting only).
   Follow-up: `TimetableMeetingPolicy.gridSlots` (shared with split view)
   tells columns apart by their raw start and end, so an English meeting
   stored at "9:00" is drawn once, in the 09:00–12:00 column, beside an
   empty "9:00–12:00" one; it should key columns by `_hhmm` too. F11 (the
   page's `isEnglish` against the server's policy) stands.
   **After review (6e):** a table with no slot column, drawn only for its
   strip, flowed its day labels two to a row (Chrome: Sun beside the
   corner, then Mon and Tue, Wed and Thu, then "Off grid"), since its one
   laid-out column took every other label. A row's label
   (`.tw-grid-slot-label`, the days and the strip's header) is now pinned
   to the day column (`grid-column: 1`); the server
   always sends lab columns, so only the page could draw such a table. The
   Chrome test `test_a_table_with_no_columns_keeps_each_day_on_its_own_row`
   draws the lab table with none and checks every table's labels (each in
   the day column, under the row before) and its strip (beside its header,
   clear of the day column); it fails without the pin, and the first Chrome
   test runs the same check on every table at every size. Three surviving
   reviewer mutants now fail: PK4b has a later course that starts earlier
   on the same day (the strip's start order), and PK7b asserts Selection's
   meetings list (each day as the grid names it: `dayHtml`, not the raw
   value) and the dash for an empty day on the card, in Issues, Selection
   and Move (`dayHtml` and `dayText`). The fold's label counts the strip's
   cards: PK1's "2 meetings" fails if it counts only cells' cards or is
   written before the strip is drawn. 6c's R-fold-label-all-cards (the
   table's meetings, not its drawn cards) is equivalent now, as every
   meeting is drawn exactly once (§5). Nothing is left of the `locate`
   option: `add()` spreads its `extra` (a board target, `pending`, a
   readiness item and its weight) and nothing reads `locate`, so re-adding
   it to `offGrid` is an equivalent mutant.
   Follow-ups (recorded, not done): the `blocked` issue still keys on the
   raw `${day}|${start}`, so a meeting stored as "9:00" or "sun" is drawn in
   its cell but a blocked time there misses it; it should use `cellKey`
   (older than 6e). An English meeting on a teaching day with a start that
   is no time ("9am") is parked, but `isPlacementOnGrid` (and the server's
   `policy_meeting`) calls every such English meeting on the grid, so it has
   no Issues row; garbage data only, with the `gridSlots` `_hhmm` follow-up
   above. Python's `\d` in `_TIME` matches any Unicode digit ("٠٩:٠٠")
   where the page's `SERVER_TIME` reads ASCII only: in theory the server
   calls such a meeting on the grid while the page parks it (every stored
   time is HH:MM; E14's "one cell key" holds for them). The strip's
   gridcells sit in a plain `.tw-park` box with no `aria-colindex`, while
   the fold's cell spans `numSlots + 1` columns: a screen reader may give
   their column positions loosely (the keyboard, by `data-ux-col`, is right
   and tested).
   **As built, 6d (the lens from the quality read):** the page reads the plan
   lens only through `quality/?lens=1` and never reads `/plan-lens/` (split
   view still does, E22). `loadPlanLens`, its read record and its revision
   are gone; `resetPlanLens` stays and now also forgets the quality read in
   flight, which carries the lens (a reload after a planner job keeps the
   data version, not the timetable). One read is in flight at a time
   (`qualityRead = {scenarioId, version, lens, promise}`, contract C): a
   request joins it when the scenario and data match and it has the lens or
   none is needed; any other starts a new read and only the newest lands. A
   read overtaken by a newer one resolves with the newer one, so a board's
   load (`boardLoading`) holds until the quality it will show (LN9); a
   caller of the older read used to resume while the newer read was still
   out. `refreshWorkspaceQuality(force, {lens = lensWanted()})`: the
   board's read passes `lensWanted()` explicitly, as the spec says, and every
   other caller gets it by default, so the Generate path asks for it too.
   `lensWanted()` is true while no lens answer (or failure) has come since the
   scenario opened or the lens was reset, or while the filter holds a
   programme or SHARED; Refresh and a saved mark pass `{lens: true}`. A
   non-forced read reuses the held quality only when the lens is not wanted
   or is current (`lensCurrent`). The answer is split
   (`const {plan_lens, ...quality} = data`): the lens never sits in
   `TW.quality`. Every path that opens another scenario resets the lens
   (`onScenarioChange`, `resetWorkspace`, and now Generate's in-place
   switch to the scenario it made, which kept the previous scenario's lens
   until a reload: LN1b), so the lens is always this scenario's.
   A lens read that fails, or answers `plan_lens: null`, keeps what the lens
   had and marks it `failed`, with `failedVersion` the data it was about;
   `programme_view: null` is a lens without a view. Neither is asked again
   by typing or by a board switch for the same data (the spec's "stale"
   would re-read on every keystroke and every switch while the lens cannot
   be built); the next edit asks again, and so does Selection's Refresh.
   **Dims by the server's roles.** `lensRoles(p, query, view)` reads
   `programme_view`: for each programme of the filter (SHARED alone: every
   programme of the view), the role its per-plan file gives the meeting
   (`shared_roles` under SHARED), or null when the meeting is unknown (no
   programme or SHARED, no roles, an id not in `placement_ids`). `inLens` is
   "any role" over the union; the interim allocation rule of 6c
   (`itemMatchesPlanToken`'s section branch) is gone, and what is left of it,
   the course branch, is the left list's own question ("does P need this
   course", E12) as `courseMatchesPlanToken`. The programme tokens are the
   view's `programmes`; before a view is held, the boards' programmes
   (upper-cased, board order; those the lens knows when a lens without a view
   is held), as E5b says. The states (`lensView`): `fresh`; `updating`, the
   data moved on since the board was dimmed for these same programmes, so
   the old roles stay (a new meeting is unknown, so not dimmed) until the
   new ones land; `checking`, no roles to show for these programmes yet (a
   programme typed while its roles are read, or after an edit made without
   it: nothing is dimmed for the lens, as the spec's table says); `failed`,
   the last read gave no view: nothing is dimmed. "The same programmes" is
   `TW.lensKeyShown`, written by `applyGridView` (the programmes and SHARED
   the cards were last dimmed for from roles that were current, or kept), so
   switching programmes while roles are read shows "checking", not roles the
   user never saw. A card's title adds its role words for each programme
   ("AI2: its own section", "… mixed section (5 of 12)", "… seats 2 of 30;
   mainly another programme's", "… outside its plan; its file lists the
   course as placed"; c and t from its section's lens row, the numbers the
   server classified it by), "(checking)" when unknown, and nothing while
   the view could not be read. The count's sentence is unchanged and the lens
   follows it as its own sentence(s): "{P}: {n} of {total} meetings in its
   timetable" per programme (n counted over the board, by the roles),
   "{P}: checking…", "{P}: updating…", or "The programme view could not be
   read; nothing is dimmed for {P}." With the seating unassessed the
   allocation sentence follows. Roles arriving change attributes and text
   only: the same cards, the focus kept, no node added or removed (LN4).
   **Deviations from the spec's words**, each to keep a sentence true: the
   shared note names its programmes ("Not a shared section in AI2's
   timetable", "… in these timetables: AI, AI2") and SHARED alone says "Not a
   shared section in any programme's timetable" (the spec's "these
   timetables" had nothing to point at); the count's lens part is a sentence
   after the count's, not the 6c clause "programme filter: {P}"; under
   SHARED it reads "{P} (shared sections only): …", and SHARED alone
   "Shared sections: {n} of {total} meetings in the programmes' timetables";
   the Arabic count after من keeps 6c's genitive plural (لقاء واحد، لقاءين، 3
   لقاءات، 11 لقاءً) where the spec wrote "{total} لقاء"; the Arabic failure
   for several programmes or SHARED is "لم يُبهَت شيء لتصفية البرامج: …" (the
   spec's "للبرنامج {P}" names one); the seating words come in three forms
   (one programme, several, none: Selection's, which is about a section, not
   a programme). The legend has two dim lines: 6c's "Dimmed: outside the
   filter" for a text dim, and `#twLegendDimLens` for a lens dim, "Dimmed: "
   and the lens's note ("Dimmed: not in AI2's timetable"), its words
   rewritten in place (a text node, so typing a programme adds no node to the
   board). The export scope names the programmes ("Programme AI2 only",
   "Programmes AI2, DS only"), "Shared sections only" and the text, joined
   by " · ", each programme isolated.
   **Selection (E20):** the 4b words; "—" plus "The seating could not be
   evaluated: sections follow the plan's allocation, as the per-plan files
   do." when the seating is unassessed (never "No student of this scenario
   sits here"); a read again that failed keeps the mix it had and says "The
   programme mix could not be read again.", then "From before your last
   change." when it is older than the data, with Refresh. A saved mark makes
   one quality read with the lens and then the publish check (LN7: one
   quality read in all, none of `/plan-lens/`, no Refresh after).
   **Names (E19):** `duplicatedCodes()` is computed once per lens (it was a
   scan of every course key per card); the board records the set it was
   drawn with (`TW.drawnDupKey`) and a lens answer draws it again only when
   the set changed (LN8: on open, drawn without names, then once with them;
   the same set again, no redraw).
   Versions: PAGE ?v=59 (`timetable-workspace.css`, `global.css` and
   `ux-controls.js` unchanged).
   **Tests:** LN1–LN11 with LN1b, LN5b–LN5d (`timetable-workspace.test.cjs`);
   FL10 now dims by the roles (a spec change); the 4b and lens-mechanics
   tests (older answer, joins, reset, older read finishing, Refresh, the
   programme filter read anew) are re-pinned on the quality read, and
   "older read finishing" now also pins that its caller waits for the newer
   read (a behaviour change); the board harness answers no `/plan-lens/`, so
   any read of it fails a test; `timetable-outcomes` gives the sliced
   Generate a `resetPlanLens`; the copy and integration harnesses serve the
   lens with the quality. Chrome: `LensTests`
   (`tests/test_timetable_workspace_lens_browser.py`; the browser support
   records every URL a page asks for). FL2 (6c) watched the board with a
   MutationObserver whose callback was handed, and so emptied, the queue
   before `takeRecords()` read it, after an await: it could see nothing.
   FL2 and LN4 now keep every record they are handed.
   Mutation-checked: 78 jsdom mutants of the page (the reading rule, the
   joins and the newest-wins rule, failures, the roles and SHARED, the
   states, every new string's form, the legend, the export scope,
   Selection, the mark save, names) and 6 Chrome mutants (the roles by the
   allocation, "other" dimmed, a `/plan-lens/` read kept, the legend line
   for the wrong dim or faded, the scope as text). 73 jsdom mutants went
   red at once. Of the 5 that survived, 3 now go red on a stronger test: a
   reset that kept the read in flight (the reset test now lets the
   forgotten read land before any other starts), the lens folding a table
   whose text matches only a meeting the lens dims (LN3, "ds cs1"), and the
   legend's words rebuilt as a node (LN4, once its observer kept its
   records). 2 were equivalent by construction, and their code is gone: a
   scenario tag on the lens (every path that opens another scenario resets
   it) and a `failedVersion` for a lens with no view (its own version
   already says it is current). Re-running the changed lines found a third
   equivalent, a loading flag the failure wrote after the read had cleared
   it, also gone (its mutant, the read's own clear, goes red). The 76 that
   remain, and the 6 Chrome mutants, all go red.
   Follow-up (recorded, not done): a lens whose programme view alone could
   not be built (`programme_view: null`, the lens itself current) offers no
   Refresh in Selection, whose mix is current; the dims say so in the
   count's description and are read again by the next edit.
   **Real data** (a copy of the QA database, production flags, the page on
   a dev server, Chrome): on scenarios 663, 665 and 666, every board and
   every board programme, with and without SHARED (120 cases), the count
   "{n} of {total}" equals the meetings of that board the programme's file
   holds by `programme_view`; each scenario made one `quality/?lens=1` read
   and no other quality or `/plan-lens/` read across its five boards and
   forty filters.
   **After review (6d):** a Refresh (or a saved mark) overtaken by a plain
   read, an edit made while it read with no programme in the filter,
   brought no lens and still said "Programme mix updated.": the lens held
   was loaded and not failed, though from before the edit (the old
   `/plan-lens/` code said the same). `refreshPlanLens` now says it only
   for a lens of the data now. A lens held that is older than the data,
   with no read failed for the data now, is "The programme mix is from
   before your last change." ("توزيع البرامج من قبل آخر تعديل."), as
   Selection says with its Refresh; a read failed for the data now, or no
   lens held (a reset), is "The programme mix is not available." as before.
   LN12 pins the three, in English and Arabic: overtaken by a plain read,
   by a read with the lens (a programme typed meanwhile: updated), and by a
   reset. Mutation-checked: 9 mutants of the announcement (each term of
   both conditions, the words in both languages) all go red, in LN12 or
   LN5c.
   Recorded, not changed: after a re-read of the same data that fails,
   Selection keeps the mix it had ("could not be read again") while the
   board drops every lens dim, as `lensView` says `failed` though the held
   roles still describe the data. That is the spec's state table ("Read
   failed → none for the lens", pinned by LN5c); a follow-up may align the
   two. The reviewer's surviving mutant, `resetPlanLens` no longer clearing
   `TW.lensKeyShown`, is equivalent: after a reset the lens is unloaded, so
   `lensView` holds no view and says `checking`, and the first lens answer
   runs `applyGridView`, which writes `lensKeyShown` before anything reads
   the old key. The line stays as a guard.
   **As built, 6f (the slot dialog):** ⋯ › Edit time slots… opens
   `#twSlotDialog`, a template dialog through `UXControls.dialog`, where the
   textarea editor was (a `dlg.confirm`, with the confirmation stacked on it;
   `openSlotEditor` is gone). It shows the scenario's effective columns as
   rows, lectures then labs, from `editorRows`: a label (`dir=auto`), a
   start and an end typed as text (`.ux-rows-time`: `inputmode=numeric`,
   `dir=ltr`, no autocomplete or spellcheck), the length (`lengthText`,
   display only), the column's rule as a pill (Online only, Policy column:
   read only, owner Q4, with the old editor's line that an online window
   keeps its rule while it keeps its times), and Up, Down and Remove, icon
   buttons named by the row (its label, else "Lecture column 3"; the name
   isolated) and `aria-disabled` where they cannot act, so the keyboard stays
   on them. Each field is named by its row header and its column header. A
   list always keeps one row (owner Q5): Remove is off on a list's only row,
   described by `#twSlotOneRow`, which shows; and Save sends nothing while a
   list is empty (the server's columns never are; an empty list would be
   saved as its defaults). Add appends a row and focuses its label; Remove
   focuses the next row's label, or Add; Up and Down keep the keyboard on
   the same button. Typing updates the row's length and its buttons' names
   in place: the rows are drawn again only when they change shape.
   **Save** (E15, contract C): the rows in their order through
   `rowsToPayload` (trimmed, an empty value sent as empty, never a flag; no
   time checked), each list's row keys kept as `sent`. `SC.save` with
   `send` (`dialogWrite`, which now also returns the server's `error`,
   E18), `invalid` (each refused field `aria-invalid` on the row sent at its
   index, the row's error line under it — "End: must be after the start",
   `fieldErrorText`, the field named as its column header — and the first
   marked field takes the keyboard; a list's own error, index −1, is the
   alert's alone), `confirm` (the review, below) and `report` (the alert).
   While the save is out the rows are `inert`, the status line says "Saving
   the columns…", the keyboard waits on the body (Save turns off under it,
   as a choice list holds it in Move), and a second submit sends nothing.
   A failure is said in the alert: the gate's words for published, a
   planner job or busy (the hold's words go when it does), else "The
   columns were not saved."; the keyboard goes back to Save unless the user
   moved to a way out. **The review** is a step of the dialog, never a
   dialog over it: the rows hide, `confirmationHtml(details, {parked:
   true})` shows under "Check before saving", which takes the keyboard,
   Back shows, Save reads "Save anyway" (the template's
   `data-review-label`). Save anyway repeats the same body with the
   server's fingerprint; the review stays in view while it is out, Back
   hidden. Back, or the dialog closing, answers the module false (the
   promise always settles); Back puts the keyboard on Save. **Saved:** the
   draft goes, the dialog closes (the keyboard back on ⋯), `TW.scenario`
   takes the answer, the board is read again (`selectBoard`, so quality
   too, E17), and the saved columns are said — "Columns saved: 7 lecture, 8
   lab." or, when `hidden_count` > 0, a warning that adds "now under their
   tables, outside the columns: 2 meetings" (`meetingsCount`) — through
   `dialogSay`, so a dialog open by then says it. **Holds:** Save is
   `data-tw-mutation`; its note gives the first hold. The ⋯ item stays off
   while the page is busy, as before, so the dialog meets a hold that starts
   while it is open. **Drafts (E15d):** page memory per scenario, written on
   every change, dropped when the rows would send what is saved, by "Start
   from the saved columns" (`#twSlotDraft`, shown on an open that has a
   draft) and by a save that lands; Cancel, Escape and a failure keep it.
   A draft keeps `base`, the saved columns it started from (every lecture
   and lab column's label, start, end and rule, as one string): an open
   whose saved columns are no longer those (saved elsewhere, e.g. split
   view, and read again with the board) still offers the draft, and its
   notice adds "The saved columns changed since this draft."
   (`#twSlotDraftChanged`, the warning ink, both arms), with "Start from the
   saved columns" as before; the draft is never offered as current. A draft edited further
   keeps its `base`; one started again from the saved columns takes theirs.
   A draft that would send what is saved now is no draft at the open.
   **Escape while the save is out** (D17a) closes the dialog; the save
   ends and says how where the user is (the slot dialog open again, another
   helper dialog, or a toast). A confirmation it then needs is not given:
   nothing more is sent and it says "The columns were not saved."; when the
   edits are kept and no slot dialog shows them, the words add "Your edits
   come back when you open “Edit time slots” again." (a sentence beyond the
   spec's strings). A refusal is never marked on the rows of a dialog opened
   again meanwhile.
   **The slot module** (`?v=5`, three templates) gains `editorRows`,
   `rowsToPayload`, `lengthText` (`pluralAr`), `rowWords` (headers, a row's
   list word, flags, Up/Down/Remove), `fieldErrorText`, `save({invalid})`
   (without `invalid` the answer is as before: SC1 and split view's test;
   a fourth answer that the list changed again is never shown, since no
   send would follow a yes to it: three reviews, then "The timetable kept
   changing…", for split view and MRI too), the reading of a typed time
   (`typedTime`: an Arabic-Indic (U+0660–0669) or Eastern Arabic-Indic
   (U+06F0–06F9) digit, as an Arabic keyboard types it, is its 0–9; used by
   `lengthText` and by `rowsToPayload` for start and end, never for a label)
   and `minutes`, the server's reading of a stored time (`_TIME`): the
   grid's `timeMinutes` (6e) now calls it, so the grid and the dialog share
   one client copy (PK5 still pins it against Python). The integration
   harness loads the module before the page, as the template does, and the
   workspace harness sets `<html lang>` as `base.html` does (the module
   reads it). **Global** (`global.css ?v=87`): `.ux-rows` with its cells,
   `.ux-field-error`, the refused field's critical edge in a dialog, and
   `.ux-dialog-wide` (§3.3); page CSS (`?v=20`): the two steps' rhythm and
   the review's heading. PAGE `?v=60`.
   **Deviations from the spec:** (1) the rows' section is `#twSlotEditor`:
   `#twSlotEdit` is the ⋯ item's id. (2) Only that section goes inert, not
   the whole body: the body (`tabindex=-1`) holds the keyboard while Save is
   off. (3) The tables' headers and a row's words are drawn from the slot
   module (one copy, which the field error reuses), not written in the
   template; the legends, Add, the hints, the review's heading and the
   buttons are template arms. (4) The field error names the field by its
   header ("End: …"), not by `FIELD` ("end"). (5) The open's refusals use
   the gate's words as warnings ("Select a scenario first";
   `REFUSAL.published`), not the old error toasts. (6) The dialog is 800px
   (`.ux-dialog-wide`); at 600px the label had 30px. (7) Cells are
   middle-aligned (the spec's CSS said top), and the length, rule and
   actions cells have minimum widths with the label taking the rest, so the
   two tables line up. (8) `{meetings}` in the toast is the page's
   `meetingsCount`. (9) A field's name is its row header, then its column
   header ("Lecture column 1 Start"), the spec's two parts in the order a
   row is read.
   **Tests:** SC1–SC6 (`timetable-slot-config.test.cjs`); SD1–SD12
   (`timetable-workspace.test.cjs`) replace the textarea editor's four tests
   (a spec change): SD1 rows, pills, names, focus, a second open refused;
   SD2 the body; SD3 marks on the rows sent after a reorder, the alert, no
   toast; SD4 the review (Back, Save anyway by fingerprint and its step,
   Escape settling, the next open on the rows); SD4b a list that changed
   while it was reviewed (the new list reviewed and confirmed by its own
   fingerprint; after four answers, three of them reviewed, the module's
   words and the rows back);
   SD5 saved; SD6 Add, Remove,
   Up, Down and the one-row rule, an empty list never sent; SD7 drafts; SD8
   holds, inert, one save at a time, the open's refusals; SD9 Arabic (times
   left to right, `pluralAr` lengths, isolated names and review times, the
   template's arms); SD10 a save the user left; SD11 a draft older than
   the saved columns, SD11b one whose column's label, start or end alone
   changed (the confirm's ask; each field's mutant goes red); SD12 a time typed in Arabic digits. Chrome
   (`tests/test_timetable_workspace_slot_dialog_browser.py`,
   `SlotDialogTests`): SDB1 at 1366×650 and 1440×790, light and dark, en
   and ar, wide font (the head and Save in view while the rows scroll, no
   sideways scroll, the tables lined up with the label taking the spare
   room, "08:00" whole in its 76px field and left to right, each field's
   edge and the refused one's critical edge at 3:1, the words at 4.5:1,
   nothing under 11px, a button that cannot act faded, Escape back to ⋯);
   SDB2 the keyboard alone (⋯, the item, an end before its start, Enter:
   marked and focused; fixed — in Arabic typed in Arabic-Indic digits, its
   length shown — Enter: saved to the database as 0–9, the online-only
   windows keeping their rule, the keyboard on ⋯); SDB3 removing the 16:00
   column that holds an ORM meeting (reviewed, Save anyway, the meeting
   under its table); SDB4 below 560px each row two lines, nothing sideways;
   SDB5 en and ar, light and dark: a draft is current after the board is
   read again from the real server, and says the saved columns changed once
   they are saved elsewhere (its words at 4.5:1), "Start from the saved
   columns" giving the new ones.
   The neutral-button inventory gains the dialog's buttons.
   Mutation-checked: 100 jsdom mutants and 15 Chrome mutants all go red but
   two, argued equivalent: the draft's defensive copy (every change to the
   rows writes the draft again, so an alias holds the same rows), and the
   session check before a left save resets `pending` and the step (a dialog
   opened again cannot be saving or reviewing while an older save is out:
   every write waits for the one out). Survivors first found and
   strengthened: the step not reset at open (SD4 now reopens while Save
   anyway is out), the click's `aria-disabled` check (removed: the row's
   own rules already refuse, and the last-row rule now goes red), a second
   submit while out (its alert and status are pinned), a second open (no
   row is drawn again), a refusal marked on a reopened dialog's rows, the
   scenario not taking the answer (skipping it; a replace for the merge
   survives, equivalent: the server answers the full scenario), and the
   label's cell not taking the spare room (each time cell is pinned at its
   field's width).
   **Review fixes (6f review, all non-blocking):** (1) M18 survived: SD7
   read the alert's text only, so a reopen that left `#twSlotAlert` shown
   and empty (a bare warning strip) passed; SD7 now pins `hidden`. (2) A
   draft had no record of the saved columns it started from, so after a
   save elsewhere the older draft came back as if current (saving it would
   replace the newer columns unless meetings or rules made the server ask):
   `base` and `#twSlotDraftChanged`, above (SD11, SDB5). (3) `save()` asked
   a fourth time on the fourth 409 and then sent nothing: the last answer is
   no longer shown (the module's test counts sends and reviews, en and ar;
   SD4b). (4) A time typed in Arabic digits had no length and the server
   refused it: `typedTime` (SC6 en and ar, SD12, SDB2 in Arabic). Mutants:
   18 jsdom (28 runs) and 5 Chrome, all red. M1 (marking by the live order
   instead of `sent`) stays, argued equivalent: the rows are inert while a
   save is out and a dialog opened again gets no marks, so the live order
   at the answer is the order sent; SD3 pins the marks against the order at
   open (its reorder is before Save), not `sent` against a later order. M34
   (a removed row's mark kept) is equivalent too: row keys are never reused.
   **Real data** (a copy of the QA database, read only): all five
   scenarios (662–666) are on the server's defaults (7 lecture and 8 lab
   columns, 3 online-only); each one's rows, sent back unchanged as the
   dialog sends them, clean to exactly the columns it has (flags carried),
   with nothing to confirm; no label is over 40 characters; and the
   module's time rule reads all 788 stored starts and ends as the server
   does.
   Follow-ups (recorded, not done): split view's editor on this dialog
   (F13); `.im-form-error` and the exam repeater on `.ux-rows` and
   `.ux-field-error` (F12). A row's rule pill is drawn once, from the saved
   flags: it stays after the row's times are edited, and a plain row given
   a flagged column's start and end shows none, though the server carries
   the rule by start and end; display only — the review names a rule lost
   before anything is saved (E15b). The ⋯ item stays off while the page is
   busy or a planner job runs, so a draft cannot be looked at then (the
   spec's "opens, Save held" is reached only by a hold that starts while
   the dialog is open). Labels past 40
   characters are cut by the server silently; the next open shows the saved
   label. Below 560px the rows are flex lines: table semantics there are
   not checked with a screen reader (the workspace's acceptance starts at
   1366px). The grid's column cells still read their start with the older
   `minutesOf` for `data-ux-col` (the same minutes for every stored column).

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
| D17a | **Escape during a commit closes the dialog.** The write finishes and its result arrives as a toast after the close, or, when another helper dialog is open by then, in that dialog's alert line (D17), led by the meeting's course and section when that dialog is about another meeting. | | |
| D17b | `twFetch` fixes: announce the planner job of `SCENARIO_BUSY` before `_onError` and `_quiet`; call `_onError(null, status)` when the body is not JSON; call `_onError(null, 0)` when fetch rejects. | Today `_onError` runs first (PAGE:204-208), a non-JSON body skips it (PAGE:212), and a rejection skips it (PAGE:219-221). | |
| D18 | Before a commit button turns off, focus moves to the checked radio. | | Commit-focus findings (both critics) |
| D18a | `aria-busy` goes only on `.ux-choice-list`. Status lines sit outside it. | | |
| D19 | **Preview reads keep one promise in flight per key**, joined again on reopen and never aborted. | The lane token is released only when the view returns (request_lanes.py:63-80). Precedent: `loadPlanLens` (PAGE:1655-1685). | Preview-lane finding (eng) |
| D19a | The memo key includes `TW.dataVersion`. Retry skips the memo but still joins a request in flight. | | |
| D20 | **Change room** reads the room list at the server's current time (no slot parameters; VIEWS:2821-2823, SVC:2152-2154). Since 5c-audit it asks for the quality audit's answer with it (`?audit=1`). | | Room findings |
| D20a | Its commit body is `{room}` only. That keeps the change room-only and keeps the lock (VIEWS:2997-3013). | | |
| D20b | A room that fits (type, gender, seats) can be chosen even when another section holds it at that time. This follows the owner ruling of 2026-10-20 (SPLIT:4254-4269). | | |
| D20c | Rooms that do not fit are listed with their reason and cannot be chosen, as in split view (SPLIT:7095-7098). Owner question Q1. **Amended by 5c-audit and applied by its client PR (the owner's decision, 2026-10-22):** judged by the quality audit's requirement when the answer carries one, a room the audit refuses for the wrong gender or the wrong type is listed with its reason and cannot be chosen; a room short of the audit's seats is a choosable warning ("Room too small"). Split view keeps the legacy rule until F3. | | |
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

`dialogSay(dialog, text, tone, about)`:
- While `UXControls.dialog.isOpen(dialog)`: put the text in that dialog's `[role=alert]` line.
- Otherwise, when another helper dialog is open (the user left this one during its commit and opened another): put the text in that one's line. Clearing (`''`) is only ever about the dialog itself.
- Otherwise: `notify[tone](text)`, as it is.
- Each helper dialog records what it is about when it opens (`data-tw-about`, as `placement:<id>`). `about` is `{key, name}` for the meeting the words are about (`name` = course and section, each isolated). In a line whose dialog is about anything but `about.key`, the words read `{name}: {text}`, in both languages, so a result never reads as being about the meeting the open dialog names (`#twRoomDialog` is one element, whichever meeting it is open on).
- The line takes the tone (`data-tone`), so a saved change never reads as a refusal.

**`twFetch` changes (PAGE:197-221)**, shipped in 5c:

```js
// Asked at most once; a handler that throws has not handled the failure
// (the slip is logged, and the toast follows as if there were no handler).
const handled = (body, status) => {
  if (asked || typeof _onError !== 'function') return false;
  asked = true;
  try { return Boolean(_onError(body, status)); } catch (slip) { console.error(…); return false; }
};
if (!response.ok) {
  let d = busyBody; if (!d) { try { d = await response.json(); } catch { d = null; } }
  const e = d?.error;
  if (e?.code === 'SCENARIO_BUSY' && e.details?.active_job) announcePlannerJob(e.details.active_job, e.details.scenario_id, e.details.active_job.job_id);
  if (handled(d, response.status)) return null;
  if (_quiet) return null;
  /* message as today */ notify.error(msg); return null;
}
/* catch (err): a rejected fetch, or a 2xx whose body is not JSON */
if (handled(null, 0)) return null; if (!_quiet) notify.error(…)
```

The local busy guard (a write while the workspace is busy) toasts and
returns null before any request, and never reaches `_onError`. The existing
`_onError` users (publish, drop, the slot editor) use no dialog or the
page's `dlg` ones, which sit under the toast stack (z-index 2000 against
3000) and hide only `main` from assistive technology, so that toast is
seen; they read `body?.`, so a null body is safe for them. A template
`<dialog>` opened modal is in the top layer, over every toast: its commit
is `data-tw-mutation` (off while busy), and `dialogWrite` checks
`workspaceCanEdit()` itself before it asks.

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
- Closes the dialog, then focuses `returnFocus()` if that element is connected, enabled, not a hidden input and not inside `[hidden]` or `.d-none`.
- Dispatches `ux-dialog-close` with `{reason}`.

```
isOpen(dialog)
```

Delegated listeners on `document`, acting **only on dialogs in the WeakMap**:
- `click` on `[data-dialog-close]` → `preventDefault` (a button with no type is a submit button), then close with reason `'cancel'`;
- capture-phase `cancel` → `preventDefault`, then close with `'cancel'`;
- capture-phase native `close` while the dialog is still in the map → do the close bookkeeping with reason `'close'`: no cancel began it (a cancel closes the dialog and leaves the map first), so it is a `<form method="dialog">` submitted or page code calling `dialog.close()`. Chromium 145 fires `cancel` on every Escape (a second one with no click between as a cancel it cannot stop), so this listener is a guard, not the Escape path;
- in fallback mode, `keydown` Escape not `defaultPrevented` → close.

**Choice list** (one delegated `keydown` in UXC):
- For a radio inside `.ux-choice-list[data-ux-submit]`, among radios of the same name that are not hidden (filtered in JS, no `CSS.escape`):
  - Home, End, PgUp and PgDn (±10, clamped) check, focus and `scrollIntoView?.()` the radio, then dispatch a bubbling `change`.
  - Enter: `preventDefault`, then `form.requestSubmit()` once, and only when the form's submit button (the first not `[data-dialog-close]`) is not `:disabled` (a disabled fieldset around it included).
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
  | an `occupied_by` entry is on `p`'s board (ids compared as text, so `2` and `"2"` are one board; a missing id is never this board) | inUse | critical | `TONE_LEAD.critical` + `ISSUE_KINDS.room` (+ " · outside this board's programme rooms" when `department_fit === false`) |
  | other `occupied_by` entries | inUse | warning | warning lead + `roomElsewhere` (+ the same pool words when `department_fit === false`) |
  | `department_fit === false` | outside | warning | warning lead + `ISSUE_KINDS.roomUnsuitable` + " · outside this board's programme rooms" |
  | otherwise | free | ok | "Free and fits" |

  The verdict reads `occupied_by` only; same-class copies are already left out by the server. `outsidePool(room)` is `department_fit === false` (a flag the answer leaves out is no fact). No verdict is `info`: every room Issues will list once saved is a warning or worse, since an info row never rings a card (§7.1).

  This table is the legacy rule. Since 5c-audit (v2) an answer that carries the quality audit's answer is judged by it instead: `verdict(room, p, target)` gives misfit (refused for gender or type), inUse, **flagged** (a free room Issues will flag once saved) or free, worded as Issues' card for it; see 5c-audit and its as-built note.
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
6. `#twRoomChoices` with three fieldsets, each a `.ux-choice-list[data-ux-submit]` (it must have that attribute): "Free and fits (n)", "Free, but outside this board's programme rooms (n)" plus its hint, and "Fits, but in use at this time (n)" plus its hint.
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
  - a hidden chosen row clears the choice, and then nothing is chosen, by the user or the dialog;
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
| Groups | Free and fits (n) · Free, but outside this board's programme rooms (n) · Fits, but in use at this time (n) · Rooms that do not fit (n) | متاحة ومناسبة (n) · متاحة لكنها خارج قاعات برامج هذه اللوحة (n) · مناسبة لكنها مستخدمة في هذا الوقت (n) · قاعات غير مناسبة (n) |
| Outside hint | Choosing one saves a “Room unsuitable” warning, which Issues then shows. | اختيار إحداها يحفظ تحذير «القاعة غير مناسبة» الذي يظهر بعدها في «المشاكل». |
| In-use hint | Choosing one saves a room clash, which Issues then shows. | اختيار إحداها يحفظ تعارض قاعة يظهر بعدها في «المشاكل». |
| Verdicts | Free and fits · {TONE_LEAD} + kind label (ISSUE_KINDS: room, roomElsewhere, roomUnsuitable) · the pool words: " · outside this board's programme rooms" | متاحة ومناسبة · (ISSUE_KINDS) · " · خارج قاعات برامج هذه اللوحة" |
| Occupants | Used by {c} ({board}) at {t}; and 1 more / and {n} more | تستخدمها {c} ({board}) في {t}؛ pluralAr {one:'وشعبة أخرى', two:'وشعبتان أخريان', few:'و{n} شعب أخرى', many:'و{n} شعبةً أخرى', other:'و{n} شعبة أخرى'} |
| Facts | Lecture room · Lab · {n} seats · {n} spare · {n} short · Male (M) · Female (F) · Building B · Wing W · Floor 2 | قاعة محاضرات · معمل · pluralAr seats {one:'مقعد واحد', two:'مقعدان', few:'{n} مقاعد', many:'{n} مقعداً', other:'{n} مقعد'} · spare {zero:'بلا فائض', one:'مقعد فائض', two:'مقعدان فائضان', few:'{n} مقاعد فائضة', many:'{n} مقعداً فائضاً', other:'{n} مقعد فائض'} · short {one:'ينقصها مقعد واحد', two:'ينقصها مقعدان', few:'ينقصها {n} مقاعد', many:'ينقصها {n} مقعداً', other:'ينقصها {n} مقعد'} · طلاب (M) · طالبات (F) · المبنى B · الجناح W · الطابق 2 |
| Misfit reasons | This meeting needs a lecture room · needs a lab · needs a room of type {c} · needs a Male (M) room · needs a Female (F) room | هذا اللقاء يحتاج إلى قاعة محاضرات · إلى معمل · إلى قاعة من نوع {c} · إلى قاعة طلاب (M) · إلى قاعة طالبات (F) |
| Loading, error, busy, retry | Finding rooms for this meeting… · The rooms could not be read. · The server is busy; try again in a moment. · Try again | جارٍ البحث عن قاعات لهذا اللقاء… · تعذّرت قراءة القاعات. · الخادم مشغول؛ أعد المحاولة بعد قليل. · إعادة المحاولة |
| Empty, none fit, truncated | No rooms are on record. · No room fits this meeting. · Showing the best 60 of {n} rooms. | لا توجد قاعات مسجّلة. · لا توجد قاعة مناسبة لهذا اللقاء. · تُعرض أنسب 60 قاعة من أصل {n}. |
| Stale | This meeting changed since its rooms were read. · Check again | تغيّر هذا اللقاء منذ قراءة قاعاته. · إعادة التحقق |
| Notes | Choose a room. · This is the current room. | اختر قاعة. · هذه هي القاعة الحالية. |
| Progress | Assigning room {c}… | جارٍ تعيين القاعة {c}… |
| Toasts | Room {c} assigned. · Room {c} assigned; {c} also uses it at this time. · Room {c} assigned; Issues will show it as unsuitable for this board's programmes. · (in use and outside) Issues will also show it as unsuitable for this board's programmes. · It also changed on {X}. — a success only when nothing is in use and the room is in the pool; else a warning | عُيّنت القاعة {c}. · عُيّنت القاعة {c}، ويستخدمها في هذا الوقت أيضاً: {c}. · عُيّنت القاعة {c}، وستعرضها «المشاكل» غير مناسبة لبرامج هذه اللوحة. · وستعرضها «المشاكل» أيضاً غير مناسبة لبرامج هذه اللوحة. · وتغيّرت أيضاً في {X}. |
| Failures | This meeting is no longer on the board. · The room was not changed. | لم يعد هذا اللقاء في اللوحة. · لم تتغيّر القاعة. |
| A result said in a dialog about another meeting (contract C) | {c} {c}: {words} | {c} {c}: {words} |

##### Tests

**Global: `timetable-accessibility.test.cjs`.**

| Test | What it checks | Mutants it must kill |
|---|---|---|
| G1 | Fallback mode: open sets `[open]` and honours `initialFocus`; `[data-dialog-close]` and an Escape `keydown` close it; focus goes to the element `returnFocus()` gives **at close**, after the opener node was replaced. | Resolver read at open |
| G2 | A cancelable `cancel` is `defaultPrevented`; the dialog closes; `ux-dialog-close` fires with `reason 'cancel'`. A native `close` without `cancel` runs the same bookkeeping, with reason `'close'`. | No capture listener; close watcher ignored; a native close reported as `'cancel'` |
| G3 | A disabled or detached target throws nothing and does not take focus. A dialog the helper did not open (`#twGenerateDialog`): its × and Escape behave natively, and the helper's handler never runs. | Listeners not scoped to the helper's dialogs |
| C1 | `.ux-choice-list[data-ux-submit]`: End, Home and PgDn (clamped) check, focus and fire `change`; hidden radios are skipped; Enter submits once (the spy counts `submit` events); Enter with the submit button disabled, or inside a disabled fieldset, submits nothing; the submitter is the commit button, past a leading `type=button` and a `[data-dialog-close]` with no type; the Page keys scroll the row, not the radio, into view; a list without `data-ux-submit` never submits. | End off by one; no clamp; hidden included; double submit; submit not opt-in; `.disabled` only; any button taken as the submitter; the radio scrolled |
| C2 | Forced colours (pinned from `global.css`, seen in Chromium with forced colours emulated): every `.ux-choice` has a `CanvasText` border, the checked row a thicker `Highlight` border, the focused row a `CanvasText` outline; the two marks never share a property. | No frame; no checked mark; checked by colour only; checked as an outline; no focus mark |
| G4 | Opening a helper dialog closes an open menu. `isOpen` is false for a dialog its page opened and for a helper dialog taken off the page, which does not block the next. | Menus left open; `isOpen` unscoped; `isOpen` ignores `isConnected` |
| G5 | A `[data-dialog-close]` with no type closes as `'cancel'` and submits nothing (the same button in a page's own dialog is left alone). A fallback Escape inside a nested page dialog leaves the helper's open. The first control skips a hidden input, a disabled button and `[hidden]`/`.d-none` content, also when `initialFocus()` is a hidden input. | No `preventDefault`; `preventDefault` unscoped; owner check dropped; any of the focus filters dropped |
| F1 | `twFetch` with `_onError` returning true on a 409 `SCENARIO_BUSY` with `active_job`: `tw:planner-job` still fires. | Announce left after `_onError` |
| F2 | A 502 with an HTML body calls `_onError(null, 502)` and fires no toast. A rejected fetch calls `_onError(null, 0)`. | Non-JSON path skips `_onError`; rejection path skips it |

**Module: new `tests/frontend/timetable-room-choice.test.cjs`.** One case table drives `fits`, `occupied`, `verdict` (en and ar) and `misfitReasons`. In `timetable-manual-clash-ruling.test.cjs` (whose harness loads SPLIT), eval the module and assert `fits(r) === roomFitsSection(r)` and `occupied(r) === roomIsOccupied(r)` over the same table. Mutants: `fits` requires `available`; tone taken verbatim from the server.

**Page: `timetable-workspace.test.cjs`.** The harness evals the module.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| RC1 | A locked meeting: the GET URL is exactly `/ops/tw/placements/10/room-candidates/`. Choose R2 and submit: the POST body deep-equals `{room:'R2'}`. The undo top is `{type:'room', old_room:'LAB25', new_room:'R2'}`. A `/boards/2/` GET and a success toast follow. | Slot parameters added to the GET; time or override sent; lock refused; no history push; no `selectBoard` |
| RC2 | R1 free; R9 occupied on board 2; R8 on another board; R7 with `department_fit:false`; R3, R4, R5 misfits: radios R1/R7/R9/R8 in groups free/outside/inUse/inUse with tones ok/warning/critical/warning; the outside and in-use groups carry their hints; misfits are static rows with words from their flags. | `free()` used for choosability; server tone used; same-board rule dropped; outside toned info or merged into free |
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
| RB4 | R105 (department CS, the board's programme AI; R100–R104 are AI's), en and ar: before the save the card names no "Room unsuitable"; R105's row is in the outside group, toned warning, with Issues' verdict and the group's hint; after Assign the report is a warning that names it, Issues lists "Room unsuitable · AI111 · R105" and the card's ring is warning. (Added after live QA, see 5c's as-built note.) | The old "For context" verdict; a success report |

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
| V10 | Without `verdict=1`, the full JSON equals an oracle captured from the base commit before the change (as built: `a5d8f96`), in every teacher mode. Query counts (`django_assert_num_queries`): legacy equals the oracle in every mode; `verdict=1` adds 0 queries, except +3 exactly with the instructor links on and one teacher per section off (see As built). Both views keep `@preview_lane`. | Any legacy drift; an extra query |
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
| Escape during a commit | The dialog closes; the result arrives as a toast, or in the dialog open by then (D17, D17a), led by the meeting's course and section when that dialog is about another (contract C). |

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

**D20c, amended (applied by the client PR; the owner's decision, 2026-10-22):**
"Rooms that do not fit, judged by the quality audit's requirement when the
answer carries one (`?audit=1`), are listed with their reason and cannot be
chosen: the wrong gender and the wrong type. A room short of the audit's seats
is a choosable warning, 'Room too small' (Q1). Split view keeps the legacy rule
until F3."
On 665 the two pages then differ on 1,679 rows that the dialog offers and
split view refuses or does not list (seats), and on none the other way: type
and gender agree everywhere on the QA data. Under the legacy seat number, 72 rows the
dialog offered were "Room too small" to Issues, and 580 it refused were
big enough.

**Q1, restated and answered by the owner** (2026-10-22). Judged by the audit,
should a room short of the audit's seat need (the buffered budget demand, or 40
when the course has no budget row) or of the wrong type be choosable as a
warning?
- **Seats: yes.** A choosable warning row, "Room too small", which Issues then
  show. The need is an estimate: a buffered forecast, and the default 40 is a
  policy number, not the section's size.
- **Type: no.** Refused (D20c): a lab meeting in a lecture room has no
  equipment.
- **Gender: no.** Refused: rooms are segregated; this is not a quality trade-off.

So the client refuses the audit reasons `gender` and `type` only
(`TimetableRoomChoice.REFUSED_AUDIT_REASONS`). The server's extended listing
keeps every row not refused for gender, so this answer (or a later change to
it) needs no server change.

**Client (`feat/workspace-room-audit-client`).** The Change room dialog reads
`?audit=1` and, before a save, says the room rows Issues will show after it;
see the as-built note under §7.2 item 5. Answers without the audit (a server
before this PR, a meeting the audit cannot judge) and scenarios whose rooms the
quality check cannot judge keep the legacy rule, byte for byte (the latter
with a note). Its new words ({c} a room code, {b} a board label, both
isolated; {k} Issues' kind labels, quoted):

| Where | English | Arabic |
|---|---|---|
| Kind (Issues' `roomSmall`) | Room too small | القاعة أصغر من المطلوب |
| Seats (Issues' `seatsNeeded`) | needs {n} seat(s) | pluralAr: يحتاج مقعداً واحداً · مقعدين · {n} مقاعد · {n} مقعداً · {n} مقعد |
| Why | outside the programme rooms of {b} · {b} has no programme · the same class stays in {c} ({b}) | خارج قاعات برامج {b} · ليس للوحة {b} برنامج · تبقى الشعبة نفسها في {c} ({b}) |
| Group, hint | Free, but lowers quality ({n}) · Choosing one saves the warning on its row, which Issues then shows. | متاحة لكنها تُضعف الجودة ({n}) · اختيار إحداها يحفظ التحذير المذكور في سطرها، ويظهر بعدها في «المشاكل». |
| Notes | The same class stays in {c} ({b}): any other room leaves it split, which Issues shows as “Room unsuitable”. · {b} has no programme, so Issues shows every room as “Room unsuitable”. · This course has no budget row, so the quality check assumes {n} seat(s). · The quality check cannot judge rooms for this meeting; rooms are grouped by type, gender and seats only. | تبقى الشعبة نفسها في {c} ({b})؛ وأي قاعة أخرى تتركها مقسّمة، فتعرضها «المشاكل» «القاعة غير مناسبة». · ليس للوحة {b} برنامج، لذا تعرض «المشاكل» كل قاعة «القاعة غير مناسبة». · لا يوجد صف ميزانية لهذا المقرر، لذا يفترض فحص الجودة {n} (pluralAr: مقعداً واحداً · مقعدين · {n} مقاعد · {n} مقعداً · {n} مقعد). · لا يستطيع فحص الجودة الحكم على قاعات هذا اللقاء؛ صُنّفت القاعات حسب النوع والجنس والمقاعد فقط. |
| Report | Room {c} assigned; Issues will show {k}. · (after the clash sentence) Issues will also show {k}. | عُيّنت القاعة {c}، وستعرض «المشاكل»: {k}. · وستعرض «المشاكل» أيضاً: {k}. |

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

**Follow-ups.** F3 (split view on the module with `?audit=1`), next; bulk
clean rooms and repair accepting only
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

- **Q1.** Change room by hand: should a room **short of seats** or **of the wrong type** become a choosable warning row ("Room too small" / "Room unsuitable" are already warnings, PAGE:3203-3204), leaving only the wrong gender refused? PR 5 keeps split view's refusal (SPLIT:7095-7098). The rule is one function, `TimetableRoomChoice.fits`. **Answered by the owner (2026-10-22), restated in 5c-audit:** judged by the quality audit, seats short of its need are a choosable warning ("Room too small"); the wrong type and the wrong gender stay refused (`REFUSED_AUDIT_REASONS = ['gender', 'type']`, applied by the 5c-audit client).
- **Q2.** Place for an **online** course: offer only the online (evening) windows? The 2026-07-28 rule for GS/GSE (timetable_autoplace.py:275-284) points that way. PR 5 offers the daytime columns, as the server does today (SVC:1428-1431), and shows a note.
- **Q3.** Change room acts on **one meeting**, as split view does. Should it offer "all N meetings of this section" when they share the current room?

### 7.4 PR 6 — what the grid shows (spec)

This spec comes from a read-only audit and an adversarial critique of the draft. The code was read at master `d4c2dc2` (PR 5e merged) and at the two open PRs that touch the same files: #181 `feat/workspace-room-audit-client` (`48e213d`) and #182 `feat/workspace-place-dialog` (`d4a166d`, which contains #181). Neither open PR changes server code.

Costs were measured on copies of the QA database (`scratchpad/pr6/probe.sqlite3`, `critic.sqlite3`) with read-only calls. "Prod flags" means `TIMETABLE_ONE_TEACHER_PER_SECTION`, `TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY` and `TIMETABLE_SECTION_PROGRAMME_PREFERENCE` on.

**Line numbers.**
- Server files, UXC, GCSS and SCJS: master `d4c2dc2`.
- PAGE, TPL, TWCSS and `tests/frontend/timetable-workspace.test.cjs` (TWT): `d4a166d`, which PR 6's client sub-PRs build on.

**Abbreviations.**
- PAGE, UXC, GCSS, TWCSS, TPL, VIEWS, SVC, POLICY, PANES and SPLIT mean what they mean in §7.3.
- BASE = `core/templates/core/base.html`
- PLENS = `core/services/timetable_plan_lens.py`
- PPE = `core/services/timetable_per_plan_export.py`
- QUAL = `core/services/timetable_scenario_quality.py`
- CE = `core/services/timetable_candidate_eval.py`
- SSA = `core/services/timetable_student_assignment.py`
- V2 = `core/services/timetable_optimizer_v2.py`
- SLOTS = `core/services/timetable_slot_config.py`
- SCJS = `static/js/timetable-slot-config.js`
- LANES = `core/services/request_lanes.py`
- REPAIR = `core/services/timetable_repair.py`
- STA = `core/services/section_teacher_assignment.py`
- MRI = `static/js/page-timetable-mri.js`

**Notation.** `{t}`, `{c}` and isolation follow §7.3. In addition:
- `{P}` is a programme code. In HTML it goes in `<bdi>`; in plain text it gets `\u2066…\u2069`, built as escapes and never as literal characters.
- `{meetings}` is a nominative count: "1 meeting" / "{n} meetings"; Arabic `pluralAr {one:'لقاء واحد', two:'لقاءان', few:'{n} لقاءات', many:'{n} لقاءً', other:'{n} لقاء'}`.
- `{meetingsAcc}` is SCJS's existing accusative `meetingsText`, used as an object.

#### 7.4.0 Fact map

**A. The plan lens and the per-plan export**

| Fact | Evidence |
|---|---|
| The lens seats on its own: profiles, sections, rigidity and tiers, then `ssa.assign_students_to_sections(…, grouping_report=)`. It reads the **mutated** `section.enrolled_student_ids`. | PLENS:65-123 |
| Any failure becomes `{}`, with no log. | PLENS:102-103 |
| Seating evidence also drives allocation: `_assign_targets_to_sections` picks owners by it. | PLENS:189-240, 270 |
| Fields: `owner`, `role`, `filter_plans` (allocation), `actual_plans` and `actual_total` (seating), `preferred_programme` and `programme_grouping` (flag). | PLENS:250-367 |
| `/plan-lens/` takes the preview lane. | VIEWS:842-859 |
| The export's programme set is the **board** programmes. | `get_scenario_programs` PPE:97-112; `_build_export_filter` PPE:196-219; files PPE:653-657 |
| Per plan-term sheet, SHARED narrows the candidate sections **before** classification. | PPE:871-887 (`_section_passes_search` :879, `classify_course_sections` :887) |
| Classification: R1 main = marked or ≥ 70 % seated; R2 mixed = no main, so the one that seats most; R4 other = seats ≥ 1. With nobody seated: allocated or marked sections are main. | PPE:267-353 |
| Out-of-plan placements are kept by SHARED plus allocation, and `_section_allocated_to_plan` also passes **UNALLOCATED** sections and sections with no lens row. They are listed only as course codes on the coverage sheet. | PPE:235-262, 912-921, 1977, 2040-2048 |
| The export seats once per export (pinned). | PPE:667; tests/test_per_plan_export_grouping.py:606-631 (spies `_section_assignment_evidence` by name) |
| The workspace token is a third, allocation-based copy, over the observed-demand token set. | `itemMatchesPlanToken` PAGE:2116-2129; `planTokenFromSearch` PAGE:2109-2114 |
| Split view and repair repeat the token rule. | SPLIT:517-531; REPAIR:277-340 |
| Measured on 665 (prod flags), sections the token shows vs the export's file. | `scratchpad/pr6/roles.py` |
| – AI: 36 vs 35 | |
| – DS: 32 vs 33 | |
| – AI2: 35 vs 40 | |
| – DS2: 28 vs 38 | |
| **Measured:** roles classified on all sections and then intersected with SHARED differ from the export's SHARED roles on 665. | `scratchpad/pr6/critic_roles.py` |
| – DS / AI / AI2 / DS2: 1 / 0 / 12 / 19 placements change role | |
| – membership agreed on the QA data; it can differ by R2's fallback | |
| The text filter matches the lens's internal words (`owner`, `role`, `filter_plans`). | PAGE:2158-2182 |
| The lens is read on open, before the boards. | PAGE:1191 |
| After that it is read only on request: Refresh (PAGE:4863), or a mark save, which runs the lens and then quality, two seatings. | PAGE:4904-4905 |
| Card names for codes that two courses share come from the lens's course keys. | `hasDuplicateCourseCode` PAGE:2131-2141, used at :2489 |

**B. Quality and the page's reading of it**

| Fact | Evidence |
|---|---|
| Order of work: rooms, then sections plus instructor metrics (**early return** on failure), then profiles, then `evaluate_generated_timetable_candidate`, then tiers and clash counts. | QUAL:153-266 (early return :224-227) |
| In one-teacher mode it runs inside `assignment_scope`. | QUAL:142-150 |
| The evaluator seats a working copy, with `grouping_report` and `reserve_shift`. | CE:48-116 |
| `reserve_shift` only records. | SSA:1129-1138, 1197-1204 |
| `/quality/` is not laned. | VIEWS:1015-1025 |
| `TW.dataVersion` is bumped by `twFetch` on a write. | PAGE:503 |
| Quality is read once per version; a board switch reuses it. | PAGE:5404-5437 |
| **Every write ends in `selectBoard`, which holds `boardLoading` until the quality read returns, and `workspaceBusy()` counts it: the next edit waits for quality.** | PAGE:1320-1366 (:1363), 331-337 |
| The workspace's GET retries a 503. MRI's `api` does not. | PAGE:476-485; MRI:39-44 |
| **Parity measured** (663, 665, 666; local flags, and prod flags inside `assignment_scope`): the lens's seating and quality's evaluation give identical per-section programme counts and grouping reports. | `parity.py`, `critic_roles.py` |
| **Silent failure measured:** on 662 and 664, `build_section_states_for_scenario` raises. Quality reports instructor `assessment_failed`, with seats unassessed and no reason. The lens reports 0 seated in every section. Selection then says "No student of this scenario sits here". | V2:232, 290; PAGE:4834-4836 |

**C. Client filter and grid**

| Fact | Evidence |
|---|---|
| The filter removes non-matching placements; groups and clashes outside it vanish. | PAGE:2251-2268 |
| The input is debounced 120 ms and calls `renderGrid()`. | PAGE:801-805 |
| One `[role=grid]` per group and kind. | `_renderSingleGrid` PAGE:2359-2441 |
| Cells match by exact strings. | PAGE:2424 |
| Board roving: `stop: '.tw-card'`. Enter is not intercepted on a native control. | PAGE:80-86; UXC:408-430 |
| Layout skips anything under `hidden`. | UXC:459-475 |
| Refresh runs on `childList` only. | UXC:686 |
| A box appearing refreshes only from no box to a box. | UXC:687-693 |
| Global disclosure: a delegated click toggles `hidden` and `aria-expanded`, keeps the choice per key, and fires no event. | UXC:249-278 |
| Tones are applied in place and are attribute-only. | PAGE:5769-5790 |
| The same-course echo is a **dashed outline**. | TWCSS:368 |
| The card anatomy needs a `tw-cell` container. | TWCSS:275, 338 |
| The day column is 56 px. | TWCSS:245, 263-271 |
| The legend is drawn inside `#twGrid`. | PAGE:2333, 2550-2564 |
| A locate moves the tab stop, not the keyboard. | PAGE:5312-5335 |
| `TONE_LEAD` words. | PAGE:5609-5613 |

**D. Off grid**

| Fact | Evidence |
|---|---|
| The server checks the day first, then a column start of the meeting's kind. Policy meetings are drawn only on teaching days. | SLOTS:195-217 |
| `_TIME` accepts `9:00` and `09:00:00`. | SLOTS:46, 92-94 |
| The page compares `slice(0,5)`; the renderer compares raw strings. | PAGE:5629-5636, 2424 |
| **An English meeting on FRI or SAT is off-grid under both rules, and the English table has no row for it.** | PAGE:2311-2318 |
| `offGrid` is a warning with `locate: false`. | PAGE:5602, 5694 |
| QA: 788 placements, all "HH:MM" on SUN–THU, 0 off-grid. | probe |
| Day and time are `TextField`s. | core/models.py:1028-1030 |

**E. Slot editor and PR 1's contract**

| Fact | Evidence |
|---|---|
| `openSlotEditor`: textareas, emoji labels, a stacked `dlg.confirm` for the 409. On success it calls `renderGrid()` only. | PAGE:5818-5889 |
| It refuses 0 lecture rows. | PAGE:5851 |
| SCJS guards a list emptied by accident ("would quietly become the defaults"). | SCJS:39-41 |
| The server takes an empty list as the defaults. | VIEWS:1035-1036; SLOTS:230-233, 300-302 |
| `VALIDATION_SLOTS` carries `details.errors[{list,index,field,message}]`. Flags are carried by start and end. A 409 `SLOT_EDIT_NEEDS_CONFIRMATION` carries `{hidden≤200, hidden_count, dropped_flags, fingerprint}`. | VIEWS:1031-1121; SLOTS:115-184, 294-346 |
| Labels are cut to 40 characters silently. | SLOTS:44, 151 |
| `SC.save` loops up to 4 times; `confirm` must resolve. | SCJS:139-155 |
| `dialogWrite` keeps no error object. | PAGE:242-256 |
| SCJS is loaded by three templates (workspace :423, split :391, MRI :220). `global.css` and `ux-controls.js` are versioned in BASE (:32, :79). | |

**F. Cost (QA copy; this machine; SQLite; 665 = 5 boards, 187 meetings, 82 sections, 401 students)**

| Read (interleaved, prod flags, 21 runs) | Median (min; p90) ms | Queries | Python peak |
|---|---|---|---|
| quality alone | 79.1 (63.2; 99.9) | 44 | 3.7 MiB |
| quality, then `/plan-lens/` | 144.2 (111.0; 178.1) | 59 | 4.8 MiB |
| one seating, with lens and roles | 105.9 (87.0; 132.9) | 62 | 4.2 MiB |

- JSON: 61 KB combined (6 KB gzipped; the app does not compress); roles 2.7 KB.
- Process: about 104 MiB after imports. The seating is not what costs memory; **latency is**, because edits wait on it (B).
- Not measured: the 0.5 vCPU host, and scenarios larger than 665.

#### 7.4.1 Decisions

| # | Decision | Evidence | Resolves |
|---|---|---|---|
| E1 | Six sub-PRs, **6a–6f**: two server, four client. Each ships alone with its tests. The client ones start after #182 merges. | 5e's size; PAGE overlap with #181/#182 | Size, conflicts |
| E2 | **No server cache. `GET …/quality/?lens=1` returns quality and the lens from one seating.** "One evaluation per data fingerprint" is realised as one seating per page data version. | Parity (B); cost (F); a fingerprint would have to cover placements, marks, demand, programmes, budgets, teacher links and flags (REPAIR:115-127 bumped twice) | Plan line |
| E2a | **The page asks for the lens only while it is wanted:** on the first quality read of an opened (or reset) scenario, while the filter holds a programme or SHARED token, on Refresh, and after a mark save. Any other read is plain `/quality/`: byte-identical and the same queries. | Edits wait on the quality read (B); §7.2 item 4 ("never re-read per edit") | Per-edit cost |
| E2b | `/quality/` stays off the lane, with or without the lens. `/plan-lens/` keeps its lane and its own seating, and gains only `seating`. | tests/test_tw_preview_lane.py:32-54; MRI has no 503 retry | Lane |
| E2c | A failure while building the lens or the roles never fails quality: `plan_lens: null` or `programme_view: null`, logged once. | QUAL's own pattern (unknown, never wrong) | Robustness |
| E3 | **One seating-evidence derivation**, `seating_evidence(states)`, from `assignment_states`. No path reads mutated sections. When quality's evaluation did not run, the lens seats itself, so it always equals `/plan-lens/`. | PLENS:105-123; CE:56; QUAL:224-227 | Two derivations |
| E4 | `seating: {assessed, students, reason?}`. A failure is logged and never reads as "nobody". | PLENS:102-103; 662 and 664 | Silent failure |
| E5 | **Programme roles: one server module, moved verbatim out of PPE.** For each placement and programme: `main`, `mixed` or `other` on its own sheet, or `outside`. The export and the lens call the same function. | PPE:867-933 | "Roles including out-of-plan" |
| E5a | `lens=1` **always** carries `programme_view`, with `roles` and `shared_roles` (the export's SHARED classification). No separate roles parameter. | Roles cost about +20 ms (F); a roles-only re-read would be a second seating; SHARED changes the roles (A) | Cost; SHARED |
| E5b | **Programmes = the export's set:** board programmes (`get_scenario_programs`), upper-cased, in board order. The page recognises a token from `programme_view.programmes`; before that, from `TW.planLens.plans ∩ board programmes`, or from the board programmes the scenario lists. | PPE:97-112, 196-219, 653-657 | Token set |
| E6 | **The lens = what P's per-plan file shows.** A card with any role for P is not dimmed. Several programmes are a union. SHARED uses `shared_roles` (alone: a union over every programme). **The client has no copy of the rule.** The lens only dims (§7.1 #5). | Owner file rule 2026-10-21 (PPE:21-29) | Semantics |
| E6a | A placement not in `placement_ids` is unknown: not dimmed, and its note says "checking". | Scenario-wide snapshot | Stale roles |
| E7 | The text filter matches what the card and Selection show, never the lens's internal words. | PAGE:2158-2163 | Filter words |
| E8 | **Text tokens fold and dim, in place.** All placements are always drawn, so the filter never rebuilds the grid (`applyGridView`). A table without a match shows its fold row. The other non-matching cards dim. | §7.1 #5; PAGE:801-805 | §7.1 #5; drag and focus kept |
| E8a | **The fold is a row of its own table, and its toggle is a roving stop** (`stop: '.tw-card, .tw-fold-toggle'`). The table's other rows sit in a `display: contents` `role=rowgroup` that is `hidden` while folded. Open state is page memory, keyed `{group}|{kind}` and cleared when the text changes. No sessionStorage key. | 5a: the board is one tab stop; UXC:459-475 | Keyboard |
| E9 | **Dim = the hue mixed down, `--t2` ink and the code at weight 500. Not dashed** (the echo is dashed), and never opacity. Rings, marks, selection, echo and focus stay. Forced colours use a dotted GrayText border. | TWCSS:295-371; §6 contrast | Dim style |
| E10 | **Dims are attribute-only:** `data-dim`, plus `aria-describedby` tokens pointing at shared visually hidden notes outside `#twGrid`, plus the `title`. A dimmed card stays a drag source and a stop; Enter, M and R work. | UXC:686 (`childList` refresh); PAGE:5769-5790 | Focus, churn |
| E11 | The global disclosure listener dispatches a bubbling `ux-disclosure` event `{open, key}` from the toggle. `roving` refreshes on it. | UXC:269-278, 686-693 | Keyboard after unfold |
| E12 | The left list stays a finder. Text hides rows; its programme token keeps the course's demand rule (a different question: "does P need this course"), over the E5b token set. | PAGE:2185-2215 | List scope |
| E13 | **Parking strip:** a last row of the meeting's own table (lecture, lab or English), with "Off grid" as its row header. **One gridcell per parked card**, ordered by day and start, `data-ux-col` = start minutes, each a `tw-cell` container. ↓ from THU lands on the card nearest in time, ←/→ walk the strip, PgDn goes to the next table. The strip is not a drop target. The Issues `offGrid` row locates. The tone stays a warning. | §7.1 #4; UXC aligned(); TWCSS:275, 338 | §7.1 #4 |
| E14 | **One cell key** for the renderer and the grid rule: `hhmm()` as SLOTS `_hhmm`, the day upper-cased to 3 letters. Shared cases gain "9:00", "09:00:00" and "sun". | PAGE:2424, 5629-5636; SLOTS:46, 92-94 | Rule parity |
| E15 | **The slot dialog** is a template `<dialog>` through `UXControls.dialog`, with editable rows. No client validation of times: the server's `VALIDATION_SLOTS` marks the field. The 409 review is a step inside the dialog, and the fingerprint is unchanged. | PAGE:5818-5889; VIEWS:1031-1121 | Textarea |
| E15a | Times are text inputs (`inputmode=numeric`, `dir=ltr`, 24-hour), never `type=time`. | GCSS:5489-5496 | |
| E15b | Flags are read-only. The server's review names a lost flag. | SLOTS:163-184, 294-333 | Q4 (owner: read-only) |
| E15c | **A list always keeps one row:** Remove is disabled on the last row, with its reason, because an empty list is the server's defaults, not "no columns". | SCJS:39-41; VIEWS:1035-1036 | No silent defaults; Q5 (owner: no defaults button) |
| E15d | **Edits are never lost:** Cancel, Escape or a failure keep a per-scenario draft. Reopening offers it, with "Start from the saved columns". | PAGE:5815-5817 ("their edits are never lost") | Data safety |
| E16 | The row model and words live in SCJS (`editorRows`, `rowsToPayload`, `lengthText`, `save({invalid})`). `.ux-rows` and `.ux-field-error` are global. Split view keeps its textarea until F13. | SCJS:1-14 | Global first |
| E17 | After a columns save the board is read again (`selectBoard`). | STA:405-411, 470-471 | Stale tones |
| E18 | `dialogWrite` also returns the server's `error` object: `{code, status, error}`. | PAGE:242-256 | Field errors |
| E19 | Course names on cards come from the lens's course keys. The grid is drawn once more only when an answer changes the set of duplicated codes. | PAGE:1191, 2131-2141 | Open order |
| E20 | Selection's programme block keeps 4b's words and Refresh. "Checking" while a lens read is out; "From before your last change" plus Refresh when stale; a failure says so, with Refresh. Refresh and a mark save make one quality read with the lens. | PAGE:4820-4846, 4896-4906 | 4b |
| E21 | The count stays "{n} of {total}", where n = cards that are not dimmed. Fold count, programme and lens state go in the title and the accessible description. | Frame test (tests/test_timetable_workspace_frame_browser.py:238) | Chrome height |
| E22 | Split view's chips and repair's plan scope keep the allocation rule until F8 and F9. | SPLIT:517-531; REPAIR:277-340 | Scope |
| E23 | The export's free-text search keeps its own rule (F10). Its programme and SHARED parts are now what the grid dims by, so "Programme AI2 only" is true from 6d on. | PPE:174-219 | Export scope |
| E24 | Asset versions follow D28. Base tips: BASE `global.css` 86 and `ux-controls.js` 5; TPL TWCSS 16 and PAGE 56; SCJS 3 in **three** templates; `timetable-room-choice.js` 2. A branch that bumps an asset another merged branch also bumped re-bumps after the merge. | BASE:32, 79; TPL:15, 422-429; split :391; MRI :220 | Assets |

#### 7.4.2 Sub-PRs

| PR | Contents | Depends on | Side |
|---|---|---|---|
| **6a** One seating for quality and lens | `?lens=1`; `seating_evidence`; `seating`; lens and roles failure isolation; budget recorded | — | server |
| **6b** Programme roles shared with the export | The new module (moved code plus `programme_placement_roles` and `programme_view`); the export calls it (workbooks equal); `programme_view` on `lens=1` | 6a | server |
| **6c** Text filter: fold and dim, in place | No removal; fold rows; attribute dims; `applyGridView`; `ux-disclosure` event; interim programme dims by today's token | #182 merged | client + UXC |
| **6d** Lens from the quality read | `lensWanted()`; no `/plan-lens/` read; dims by roles; E20 states; names; count, scope and legend | 6b, 6c | client |
| **6e** Parking strip | Strip rows (lecture, lab, English); locate; one cell key; parity cases; SCJS `{parked}` words | #182 merged | client + SCJS |
| **6f** Slot dialog | `#twSlotDialog`; SCJS editor helpers; global `.ux-rows` and `.ux-field-error`; `dialogWrite` with `error`; draft; re-read | 6e | client + global |

**Order:**
- 6a and 6b can start now, on master.
- 6c and 6e can run in parallel once #182 has merged. Both edit `renderGridWithSlots` and bump PAGE, so the later one rebases and re-bumps.
- 6d comes after 6b and 6c; 6f after 6e.

**Every sub-PR:**
- the §6 frame acceptance;
- its own bumps (E24);
- every new test mutation-checked;
- an "As built" note under §7.2 item 6;
- §3.3 and §5 updated for new globals and contracts.

#### 7.4.3 Contracts

##### A. The quality read with the lens (6a; `programme_view` from 6b)

```
GET …/quality/          -> {ok, …quality}                       // byte-identical to today, same queries
GET …/quality/?lens=1   -> {ok, …quality, plan_lens|null}       // one seating; plan_lens last
GET …/plan-lens/        -> {ok, plan_lens}                      // own seating, laned; adds `seating` only
```

- `lens` counts only when it is exactly `"1"`.
- `plan_lens` equals `/plan-lens/` for the same data, plus `programme_view` (6b).
- `seating`:
  - `{"assessed": true, "students": n}`, where n = distinct students seated in at least one section;
  - `{"assessed": false, "students": 0, "reason": "assessment_failed"}`;
  - no student demand: `{"assessed": true, "students": 0}`.
- A lens build failure gives `plan_lens: null`; a roles failure gives `programme_view: null`. Each is logged once, and quality is unchanged.
- The page splits the answer: `const { plan_lens, ...quality } = data`.

##### B. `programme_view` (6b)

```
programme_view = {
  programmes:     ["DS","AI","AI2","DS2"],                       // E5b: board programmes, board order
  roles:          { P: { main:[ids], mixed:[ids], other:[ids], outside:[ids] } },   // ascending; empty lists omitted
  shared_roles:   { P: { … } },                                  // the export's classification under SHARED
  placement_ids:  [ids],                                         // every placement of the scenario at this read
  seating_assessed: bool                                         // false: roles follow allocation and marks
}
```

Each role answers "is this placement in P's per-plan file, and as what". `outside` means its course is listed on P's coverage sheet as placed outside the plan.

##### C. Reading rule (6d)

- `lensWanted()` is true when any of these holds:
  - no lens has been read for this scenario since it opened or was reset;
  - `filterQuery()` has programmes or SHARED;
  - a Refresh or a mark save asked for it.
- `refreshWorkspaceQuality(force, {lens})` keeps one read in flight: `{scenarioId, version, lens, promise}`.
  - A request joins it when the scenario and version match and (it has the lens, or the lens is not needed).
  - Otherwise a new read starts, and the newest revision wins.

##### D. Dims, folds and the view (6c, 6d)

- `filterQuery()` = `{programmes, shared, text}`: a token is a programme when it is in the E5b set, and `SHARED` is the shared token.
- `textMatch(p)`: every text token is in `placementText(p)` (E7).
- `inLens(p)`:
  - `null` when there are no programme or SHARED tokens, roles are missing, or the id is not in `placement_ids`;
  - otherwise membership in the union of `roles[P]` (or `shared_roles[P]` with SHARED; SHARED alone means every programme).
- `applyGridView()`, in place, never a rebuild, runs after a draw, a filter change, a tone change and a lens change. It sets:
  - `data-dim` (`filter` and/or `lens`, absent when none);
  - the `aria-describedby` tokens `twDimNoteFilter` and `twDimNoteLens`;
  - the title suffix;
  - each table's fold row (`hidden`, label, tone counts, written only when the text differs) and its rowgroup's `hidden`;
  - each group heading's "N matches";
  - the count;
  - the legend's dim item `hidden`.
- Table structure:
  - `[role=grid]` > `div.ux-grid-row.tw-fold-row[role=row][hidden]` > `div.tw-fold-cell[role=gridcell]` > `button.ux-disclosure-toggle.tw-fold-toggle[aria-expanded][aria-controls=twFoldBody-{i}][data-fold-key="{group}|{kind}"]`;
  - then `div.tw-fold-body[role=rowgroup][id]`, holding the header row, the day rows and the strip row.
- `openFoldOf(card)` removes `hidden`, sets `aria-expanded`, records the open state and dispatches `ux-disclosure`.

---

#### 6a: one seating for quality and lens (server)

##### Server

**PLENS:**
- `SeatingEvidence` (frozen): `assessed`, `students`, `counts: {(course_key, label): Counter[programme]}`, `grouping: dict`, `reason`.
- `seating_evidence(states, *, grouping) -> SeatingEvidence`. This is the only derivation: for each state, each `section_id` containing `_` adds one to the student's programme (`_student_program_map`, one query).
- `_section_assignment_evidence(scenario_id) -> SeatingEvidence` keeps its name, so the export's pin is unchanged.
  - It uses the `(states, _)` that `assign_students_to_sections` returns.
  - No profiles or no sections: `assessed=True, students=0`.
  - An exception: `logger.exception`, then `assessed=False, reason="assessment_failed"`.
- `build_scenario_plan_lens(scenario_id, *, seating=None, programme_view=False)`: with `seating` given it does not seat. The payload gains `seating`; every other field and the field order are unchanged.

**QUAL:**
- `build_scenario_quality(scenario_id, *, with_lens=False)`. Inside the same one-teacher scope, after quality:
  - the evaluation ran: `seating_evidence(evaluation.assignment_states, grouping=evaluation.quality_score.get("programme_grouping") or {})`;
  - no demand: `assessed=True, students=0`;
  - it never ran or it raised: the lens seats itself (E3), which fails cheaply when the sections cannot be built.
- The lens build is wrapped: a failure gives `plan_lens: None`, logged.

**VIEWS:** `tw_scenario_quality_view` reads `lens == "1"`. The answer is `_ok({**quality, "plan_lens": lens})`. No lane.

##### Budget (As built; same method as F)

- On 665, interleaved: `lens=1` ≤ quality median +25 % and ≤ 70 % of the two separate reads.
- Python peak ≤ +1 MiB.
- Queries: quality plus exactly the lens's own reads (SQ3).
- Without `lens`: identical queries (SQ1).

##### Tests (`tests/test_tw_scenario_seating.py`)

| Test | What it checks | Mutants it must kill |
|---|---|---|
| SQ1 | No parameter: the JSON equals an oracle recorded at the base commit (key order included) in three flag modes; the query count equals the oracle's. | `plan_lens` always added; key order changed; an extra query |
| SQ2 | `?lens=1`: `plan_lens` deep-equals `/plan-lens/` with a marked section in both flag modes. `assign_students_to_sections` (`wraps`) runs once. | A second seating; grouping from the wrong run; evidence from the mutated sections |
| SQ3 | Query count = SQ1 + N, with N and the SQL prefixes pinned. | Lens inputs loaded twice |
| SQ4 | `build_section_states_for_scenario` raises: quality as today; `seating` is `{false, 0, "assessment_failed"}` on both endpoints; one log each. | `assessed` true; reason dropped; silent `{}` |
| SQ5 | No demand: `{assessed: true, students: 0}`. | Reported as a failure |
| SQ6 | `lens=true`, `lens=0` and `lens=` give no `plan_lens`. | Truthy parsing |
| SQ7 | The lane lists gain "neither: quality". | Quality laned |
| SQ8 | Instructor metrics raise while the sections build: `plan_lens` still equals `/plan-lens/` (its own seating). | `assessment_failed` without trying |
| SQ9 | `build_scenario_plan_lens` raises on the combined path: HTTP 200, quality intact, `plan_lens: null`, one log. | Quality lost to a lens failure |

##### Risks

1. **The lens is per read, not per edit.** Plain edits pay nothing (E2a). While a programme token is in the filter, each edit pays about +34 % (F), and edits wait on it. This is recorded.
2. **JSON** grows by about 55 KB uncompressed on 665 when the lens is asked (F15).

---

#### 6b: programme roles shared with the export (server)

##### Server

**New `core/services/timetable_programme_view.py`.** It imports neither OR-Tools nor the export.

Moved **verbatim**, with PPE re-importing every name:
- `MAIN_SECTION_SHARE`, `ROLE_*`, `SectionSeat`, `_lens_seating`, `classify_course_sections`;
- `_section_allocated_to_plan`;
- `_section_passes_search`, which takes `shared_only: bool` (PPE keeps a one-line adapter);
- `get_plan_term_map`, `get_plan_elective_terms`, `_classify_placement_term`, `_placement_plan_term`, and the course-key helpers they need.

Patching a moved name through `per_plan.*` no longer reaches the caller. Today's tests patch only `build_scenario_plan_lens` (tests/test_per_plan_export_grouping.py:235, 623).

New:

```python
ROLE_OUTSIDE = "outside"

def programme_context(program, scenario, course_keys) -> tuple[plan_map, elective_terms, elective_fills]   # PPE:680-688 moved
def programme_placement_roles(program, placements, *, plan_map, elective_terms, board_terms, plan_lens,
                              shared_only=False) -> dict[int, str]                                     # PPE:871-923 moved
def programme_view(scenario, plan_lens) -> dict                                                          # contract B
```

`programme_placement_roles` keeps the export's order of work:
1. SHARED narrows each sheet's sections;
2. classify;
3. an out-of-plan placement is `outside` when it passes SHARED and `_section_allocated_to_plan`.

`programme_view`:
- loads placements (`select_related("term_section")`), board terms and course keys once;
- per board programme, one context and two role maps (`shared_only` false and true);
- `seating_assessed = plan_lens["seating"]["assessed"]`.

**PPE `_build_program_workbook`** gets its keep set from `programme_placement_roles(..., shared_only=export_filter.shared_only)`:
- `is_other` = role `other`;
- the text checks are unchanged (PPE:910-927).

**PLENS:** `programme_view=True` adds the key. Only `quality/?lens=1` sets it.

##### Tests (`tests/test_tw_programme_view.py`)

| Test | What it checks | Mutants it must kill |
|---|---|---|
| PV1 | For every scenario of `test_per_plan_export_grouping.py` and `_electives.py`, with `search` in {"", "SHARED", "AI2", "AI2 CS1", "SHARED DS"}: per-sheet digests of values, fills, fonts, merges and names equal an oracle recorded at the base commit with lxml blocked (`tests/fixtures/per_plan_export_digests.json`, plus a dump tool that names the first difference). | A non-verbatim move; SHARED applied after classification; out-of-plan without the shared check |
| PV2 | Marked → main; 70 % → main and 69 % → not; mixed; other; outside (allocated, and **unallocated**); none → absent. | `filter_plans` used; the 0.70 boundary; `outside` missing for UNALLOCATED |
| PV3 | An elective on slots 8 and 10: each placement takes its own sheet's role. | Roles per course |
| PV4 | A fixture where non-shared S1 seats 2 AI2 and shared S2 seats 0 AI2 but is allocated to AI2: `shared_roles.AI2` holds S2 as main; `roles.AI2` does not hold S2. A mixed/other pair changes under SHARED. | Client-side intersection (one `shared` list) |
| PV5 | `programmes` = board programmes in board order, upper-cased; an observed-only plan and a `scenario.programs`-only programme are absent. | The union; lexical order |
| PV6 | Seating unassessed: roles follow allocation and marks; `seating_assessed: false`. | Empty roles |
| PV7 | `lens=1` adds exactly 4 + 2 × programmes queries over 6a's count. | A query per placement |
| PV8 | `classify_course_sections` and `programme_placement_roles` are defined only in the new module. The export calls the roles function once per file (spy). | A second copy |

Budget (As built): on 665, `lens=1` ≤ quality median +45 % (critique: +34 %), ≤ 80 % of the separate reads, and `programme_view` JSON ≤ 6 KB.

##### Risks

1. **A shared section on two boards can have two roles**, one per sheet. That is correct, and PV3 pins it.
2. **Moved names:** PV8 keeps one definition.

---

#### 6c: text filter folds and dims, in place (client)

##### Global (UXC; BASE `?v` 5→6)

- The disclosure listener, after toggling: `toggle.dispatchEvent(new CustomEvent('ux-disclosure', {bubbles: true, detail: {open, key}}))`.
- `roving` listens for `ux-disclosure` on its region and calls `refresh()`.
- §3.3 DISCLOSURE and ROVING say so.

##### Markup and behaviour (PAGE)

- `renderGridWithSlots` draws every placement. Each table has its fold row and its rowgroup (contract D). After the draw it calls `applyGridView()`.
- `boardNav` uses `stop: '.tw-card, .tw-fold-toggle'`.
- The `#twSearch` input (debounced 120 ms) calls `applyGridView()`, `renderBudgetSidebar()` and `updateSearchStatus()`. **No `renderGrid()`.**
- A table folds when the text is not empty and no placement in it matches. An empty table folds too, with "0 meetings".
- Fold open state:
  - `TW.foldOpen` (a Set), cleared when the text changes;
  - a click records it (from `ux-disclosure`);
  - a locate opens the fold first (`openFoldOf`, in `highlightWorkspacePlacements` before `boardNav.active`).
- Fold tone counts come from `buildBoardIssues().byPlacement`, through `applyCardTones` → `applyGridView`.
- `placementText(p)` = `placementSearchText` without `lensBits`.
- The interim lens: programme tokens dim by `itemMatchesPlanToken` (`data-dim="lens"`), never fold. 6d replaces it.
- `#twDimNoteFilter` and `#twDimNoteLens` are visually hidden `<span>`s in `.tw-centre`, outside `#twGrid`.

##### Page CSS (TWCSS)

```css
.tw-card[data-dim] { background: color-mix(in srgb, var(--tw-card-hue) 30%, var(--surface)); color: var(--t2); }
.tw-card[data-dim] .cc { color: var(--t2); font-weight: 500; }
.tw-card[data-dim] .ci, .tw-card[data-dim] .tw-card-name { color: var(--t2); }
html[data-theme="dark"] .tw-card[data-dim] { background: color-mix(in srgb, var(--tw-card-hue) 6%, var(--surface)); }
@media (forced-colors: active) { .tw-card[data-dim] { border: 1px dotted GrayText; } }
.tw-fold-body { display: contents; }
.tw-fold-cell { grid-column: 1 / -1; padding: 4px 8px; background: var(--surface); }
.tw-fold-toggle { width: 100%; justify-content: flex-start; gap: 8px; font-size: var(--text-xs); }
.tw-fold-tones { display: inline-flex; gap: 6px; font-variant-numeric: tabular-nums; }
```

The ring rules (TWCSS:347-351) must still win: a dimmed card keeps `--tw-ring`.

##### Keyboard

- A folded table offers one stop, its toggle.
- ↓ or PgDn from the previous table reaches it. Enter (native) unfolds; the event triggers a refresh and ↓ enters the table.
- A dimmed card is an ordinary stop.

##### States

| State | Board |
|---|---|
| No text | No fold rows; no `filter` dims |
| Text matches nothing | Every table folded; "0 of N" |
| A fold holds a critical card | Its toggle shows the critical icon and count, with the words in its label |
| A tone changes later | Counts update in place; same nodes |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Fold label | {table} · no match · {meetings} | {table} · لا شيء مطابق · {meetings} |
| Fold tones (words) | `TONE_LEAD`: {n} | `TONE_LEAD`: {n} |
| Dim note (filter) | Outside the filter | خارج التصفية |
| Legend | Dimmed: outside the filter | باهتة: خارج التصفية |
| Count title | {n} of {total} meetings match; tables folded for no match: {k}. | {n} من {total} لقاء مطابق؛ الجداول المطويّة لعدم وجود مطابق: {k}. |

##### Tests

`timetable-accessibility.test.cjs`:

| Test | What it checks | Mutants it must kill |
|---|---|---|
| D1 | One bubbling `ux-disclosure` with `{open, key}`; roving reaches a region just shown; `applyCardTones` does not refresh roving (spy). | No event; no listener; an attribute observer |

`timetable-workspace.test.cjs`. The tests at :1017, :1279 and :1533 now assert `data-dim`, not absence (a spec change):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| FL1 | "CS1": tables without a match folded (`hidden` rowgroup, `aria-expanded=false`); non-matches in the open tables dimmed and described by `twDimNoteFilter`; every placement in the DOM. | Removal kept; per-group fold; no dim |
| FL2 | Typing keeps the **same card nodes** and a focused card keeps focus. | `renderGrid` on input |
| FL3 | A fold's critical count; a later warning updates the count without a rebuild. | Counts ignore tones |
| FL4 | Open a fold, type: it is folded again. Same table, same text, after a tone redraw: still open. | Memory across queries; a tone redraw shuts it |
| FL5 | A locate inside a fold opens it, pulses the card and sets the tab stop. | Fold left shut |
| FL6 | "UNALLOCATED" and "core" match nothing; "No room" and a teacher's name match. | Lens words kept |
| FL7 | ↓ from the previous table lands on the toggle; Enter opens; ↓ enters the table; no fold toggle is a Tab stop outside roving (`tabIndex -1` when not active). | Toggle outside roving |
| FL8 | A dimmed card: draggable, Enter selects, M opens Move. | Dim disables |
| FL9 | Count and title in en and ar (`pluralAr` for 1, 2, 3, 11). | `plural()` for Arabic |
| FL10 | Interim "AI2" dims and never folds. | The lens folds |

Chrome (`tests/test_timetable_workspace_grid_view_browser.py`):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| FB1 | 1366×650 and 1440×790, light and dark, en and ar: dimmed code and room ≥ 4.5:1; a dimmed critical ring ≥ 3:1; the fold label ≥ 12 px and ≥ 4.5:1; a dimmed card is distinguishable from an echoed one (no dashed border); no horizontal page scroll. | Opacity dim; dashed dim; ring lost |
| FB2 | Keyboard only: ↓ to a toggle, Enter, ↓ into the table; the accessibility tree shows an expandable button inside the grid. | Event or refresh removed |

##### Risks

1. Every placement is drawn (≤ 187 cards per board on 665, as with no filter today).
2. A sessionStorage key per query is avoided on purpose: page memory only.

---

#### 6d: the lens from the quality read (client)

##### Behaviour (PAGE)

- **Removed:** `loadPlanLens`, `planLensRead` and its revision, and the `/plan-lens/` read on open (PAGE:1191). `resetPlanLens` stays. `refreshPlanLens` becomes `refreshWorkspaceQuality(true, {lens: true})` plus the announcement.
- `refreshWorkspaceQuality(force, {lens})`, per contract C:
  - URL `…/quality/` or `…/quality/?lens=1`;
  - `TW.quality = quality`;
  - with a lens: `TW.planLens = {...plan_lens, loaded: true, version}`;
  - `plan_lens: null` or a failure: the lens keeps what it had and is marked `failed`;
  - then `applyGridView()`, `renderBudgetSidebar()` when a programme token is set, and `renderGrid()` only when the duplicated-code set changed (E19).
- `selectBoard`'s quality read passes `{lens: lensWanted()}`.
- Filter input: when `lensWanted()` and the lens or its `programme_view` is stale (`version !== TW.dataVersion`), one forced read with the lens, joined per contract C.
- Dims follow `inLens(p)`. `twDimNoteLens` says the programme(s). The title carries the role words. `null` means no dim and "(checking)".
- Selection (E20): the 4b words. `seating.assessed === false` reads "—" plus the "seating unknown" words, never "nobody". Refresh appears when stale or failed.
- `saveWorkspaceProgrammeMark`: after a changed save, one `refreshWorkspaceQuality(true, {lens: true})`.
- `#twExportScope` names the programme tokens, "Shared sections only", or the text.

##### States

| State | Count | Dims |
|---|---|---|
| Token, roles fresh | "{n} of {total}"; title "{P}: …" | By roles |
| Token typed, read out | "{n} of {total}" by text only; title "{P}: checking…" | None for the lens |
| Read failed or `plan_lens: null` | Text-only count; title "could not be read" | None for the lens |
| Seating unassessed | By roles; title with the allocation words | By roles |
| Edit in flight | Old roles; title "updating…" | Old roles; new ids not dimmed |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Dim note (one) | Not in {P}'s timetable | ليست في جدول {P} |
| Dim note (several) | In none of these timetables: {P}, {P} | ليست في أيٍّ من جداول: {P}، {P} |
| Dim note (shared) | Not a shared section in these timetables | ليست شعبة مشتركة في هذه الجداول |
| Roles (title) | {P}: its own section · {P}: mixed section ({c} of {t}) · {P}: seats {c} of {t}; mainly another programme's · {P}: outside its plan; its file lists the course as placed | {P}: شعبة خاصة به · {P}: شعبة مختلطة ({c} من {t}) · {P}: فيها {c} من {t}؛ ومعظمها لبرنامج آخر · {P}: خارج خطته؛ يذكر ملفه المقرر بين المقررات المطروحة |
| Unknown | (checking) | (جارٍ التحقق) |
| Count title | {P}: {n} of {total} meetings in its timetable · {P}: checking… · {P}: updating… | {P}: {n} من {total} لقاء في جدوله · {P}: جارٍ التحقق… · {P}: جارٍ التحديث… |
| Unavailable | The programme view could not be read; nothing is dimmed for {P}. | تعذّرت قراءة عرض البرامج؛ لم يُبهَت شيء للبرنامج {P}. |
| Seating unknown | The seating could not be evaluated: {P}'s sections follow the plan's allocation, as its per-plan file does. | تعذّر تقييم توزيع الطلاب على الشعب: تتبع شعب {P} توزيع الخطة، كما في ملفه لكل برنامج. |
| Legend | Dimmed: not in {P}'s timetable | باهتة: ليست في جدول {P} |
| Export scope | Programmes {P}, {P} only · Shared sections only | البرامج {P}، {P} فقط · الشعب المشتركة فقط |

`{c}` = `actual_plans[P]` and `{t}` = `actual_total`, both from the section's lens row.

##### Tests

The 4b and lens-mechanics tests at TWT:1337-1700 move from `/plan-lens/` reads to `quality/?lens=1` reads (a spec change). That includes :1416 "older answer", :1644 "joins", :1658 "reset" and :1671 "older read finishing", re-pinned on the quality read.

| Test | What it checks | Mutants it must kill |
|---|---|---|
| LN1 | Open: no `/plan-lens/`; the first quality read has `lens=1`. An edit with no token: a plain `quality/` (no `lens`). With "AI2" in the filter: every quality read has `lens=1`. | Lens on every read; lens read on open kept |
| LN2 | "AI2": a card allocated to AI2 with no role is dimmed; a card with role `other` is not; notes and titles as in the table. | `filter_plans` used; `other` dimmed |
| LN3 | "AI2 DS" is a union; "SHARED AI2" uses `shared_roles` (fixture: a card in `roles.AI2` ∩ shared but absent from `shared_roles.AI2` is dimmed); "AI2 CS1" folds by CS1 only; "AI2" → "DS" reads nothing. | Client intersection; the lens folds; a read per switch |
| LN4 | Roles arriving: same nodes, focus kept. | Rebuild on lens |
| LN5 | Failure or `plan_lens: null`: quality shown, no lens dims, Selection Refresh forces a lens read. `seating_assessed: false`: dims by roles plus the allocation words. | Quality lost; "nobody" |
| LN6 | An id not in `placement_ids`: not dimmed, "(checking)". | Unknown dimmed |
| LN7 | Mark save: exactly one `quality/?lens=1`, no `/plan-lens/`; "checking", then fresh, with no Refresh. | Two reads |
| LN8 | Duplicated codes: names after the lens (one redraw); the same set again: no redraw. | Redraw every answer |
| LN9 | A read in flight without the lens plus a token typed: a second read with the lens starts; one with the lens is joined. | Join drops the lens; a duplicate read |
| LN10 | Arabic: isolation (`\u2066`), `pluralAr`, the scope line. | No isolation |

Chrome (`LensTests`): en and ar, light and dark; type "AI2". The DS-only card is dimmed and the "other" card is not. Export reads "Programme AI2 only". Exactly one `quality/?lens=1`, no `/plan-lens/`.

##### Risks

1. "AI2" is a programme token, so a code prefix needs a longer token ("AI23").
2. With a token set, edits wait about +34 % longer (F). That is accepted, and the user chose it.
3. A board programme with no plan requirements has only `outside` roles.

---

#### 6e: parking strip (client)

##### Markup and behaviour (PAGE)

- `cellKey(day, start)` = `${String(day).toUpperCase().slice(0,3)}|${hhmm(start)}`, where `hhmm` mirrors `_hhmm`. Both the renderer and `isPlacementOnGrid` use it.
- Each placement that is not on the grid goes to its group's table of its kind (`isLabPlacement`; English by `isEnglish`). An English meeting off the grid is one on a non-teaching day. The English table's columns come from `gridSlots` over its on-grid meetings.
- `_renderSingleGrid` appends inside the rowgroup, only when there are any:

```html
<div class="ux-grid-row tw-park-row" role="row">
  <div class="tw-grid-slot-label tw-park-head" role="rowheader">Off grid<span class="visually-hidden"> — outside the grid's columns</span></div>
  <div class="tw-park">
    <div class="tw-park-cell" role="gridcell" data-ux-col="{start minutes}" data-focus-key="park:{group}:{kind}|{id}">
      …createPlacementCard(p) with <div class="tw-card-when"><bdi dir="ltr">{day} {t}</bdi></div>…
    </div>…
  </div>
</div>
```

- Cells are ordered by day (SUN…SAT), then start, then course. There are no drag listeners on the strip.
- Day words: SUN–THU as `DAY_LABELS`; FRI and SAT named (Fri / الجمعة, Sat / السبت); anything else is the raw value, isolated.
- Issues: `offGrid` drops `locate: false`, and its detail adds "under its table".
- Legend: one item when any strip is drawn.
- SCJS: `confirmationHtml(details, {parked = false})`. The workspace passes `true`. Without the option the output is byte-identical (split view).

##### Page CSS (TWCSS)

```css
.tw-park { grid-column: 2 / -1; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 3px;
  padding: 3px; background: color-mix(in srgb, var(--sev-warning) 6%, var(--card)); border-bottom: 1px solid var(--line); }
.tw-park-cell { container: tw-cell / inline-size; min-width: 0; }
.tw-park-head { white-space: normal; overflow-wrap: anywhere; line-height: 1.2; }
.tw-card-when { font-size: var(--text-2xs); font-variant-numeric: tabular-nums; }
```

##### Keyboard

- ↓ from THU goes to the parked card nearest in start time; ←/→ walk the strip (mirrored in RTL); PgDn goes to the next table.
- M opens Move with the manual offer. R as usual. A drag onto a cell moves the card.

##### Strings

| Where | English | Arabic |
|---|---|---|
| Row header (visible + hidden) | Off grid — outside the grid's columns | خارج الشبكة — خارج أعمدة الجدول |
| Issues detail | … · under its table | … · أسفل جدوله |
| Legend | Under a table: its meetings outside the columns (M moves one onto the grid) | أسفل الجدول: لقاءاته خارج الأعمدة (المفتاح M ينقل أحدها إلى الجدول) |
| Confirmation lead (parked) | This change leaves {meetingsAcc} outside the grid's columns. They stay in the timetable and are still published; each is shown under its table until a column has its time again: | هذا التغيير يترك {meetingsAcc} خارج أعمدة الجدول. تبقى في الجدول وتُنشر، ويظهر كلٌّ منها أسفل جدوله حتى يعود عمود بوقته: |

##### Tests

The test at TWT:907 changes: the row now locates (a spec change).

| Test | What it checks | Mutants it must kill |
|---|---|---|
| PK1 | MON 18:30 lecture: in no cell; in the lecture strip with its when-line; its Issues row locates (pulse plus tab stop). | `locate:false` kept; no strip |
| PK2 | A lab in S2 goes to S2's lab strip; a SAT English meeting goes to the English strip; no strip elsewhere. | Wrong table; English unparked |
| PK3 | No off-grid meeting: no `.tw-park-row`. | Empty strip |
| PK4 | ↓ from THU 09:00 reaches the nearest parked card; ←/→ walk; `dragover` on the strip is not `defaultPrevented`. | A drop target; stops unreachable |
| PK5 | `grid_rule_cases.json` gains "9:00"/"10:15", "09:00:00" and "sun". Python and jsdom agree. Each case is drawn in a cell exactly when it is on-grid, and otherwise in a strip. | `slice(0,5)`; a raw renderer match; the day not upper-cased |
| PK6 | `confirmationHtml(X, {parked: true})` has the parked words (en, ar); without the option it is byte-identical. | Split's words changed |
| PK7 | Arabic: the when-line is `bdi dir=ltr`; SAT named; an unknown day isolated. | No isolation |
| PK8 | A parked card's teacher shows at ≥ 130 px (the container query holds). | No `tw-cell` container |

Chrome (`ParkingTests`; an ORM meeting at MON 18:30), at 1366×650 and 1440×790, en and ar:
- the strip sits under its table;
- the row header wraps without clipping at 12 px;
- contrast ≥ 4.5:1;
- M → 09:00 moves the card into a cell and the strip goes.

##### Risks

1. No live instance in QA: fixtures and an ORM-made meeting only.
2. Page `isEnglish` vs server `policy_meeting` (F11).
3. SCJS bump in three templates.

---

#### 6f: slot dialog (client + global)

##### Global (GCSS, beside `.ux-field` :2389; BASE `?v` 86→87)

```css
.ux-rows { width: 100%; border-collapse: collapse; }
.ux-rows th { padding: 4px 6px; color: var(--t2); font-size: var(--text-xs); font-weight: 600; text-align: start; border-bottom: 1px solid var(--line-strong); }
.ux-rows td { padding: 4px 6px; vertical-align: top; border-bottom: 1px solid var(--line); }
.ux-rows-time { inline-size: 76px; min-inline-size: 76px; direction: ltr; text-align: center; font-variant-numeric: tabular-nums; }
.ux-rows-actions { white-space: nowrap; text-align: end; }
.ux-field-error { margin: 2px 0 0; color: var(--sev-critical); font-size: var(--text-xs); font-weight: 600; }
.ux-dialog [aria-invalid="true"] { border-color: var(--sev-critical); box-shadow: inset 0 0 0 1px var(--sev-critical); }
@media (max-width: 560px) { /* each row a two-line card: label; then start, end, length, actions; header row visually hidden, inputs keep their labels */ }
```

§3.3 adds both components and names `.im-form-error`, the exam repeater and `.field-error` for F12.

##### SCJS (`?v` 3→4 in three templates)

- `editorRows(slots)` returns `{key, label, start, end, flags}`, with keys stable for the dialog's life.
- `rowsToPayload(rows)` returns `[{label, start, end}]`, trimmed, with '' kept and no flags.
- `lengthText(start, end)` is display only.
- `save({send, confirm, report, invalid})`: with `invalid`, a `VALIDATION_SLOTS` answer calls `invalid(errors)` and then `report(errorsText(errors))`. Without `invalid`, behaviour is byte-identical.

##### Template (TPL; copy in `{% if LANGUAGE_CODE == 'ar' %}` arms)

```html
<dialog id="twSlotDialog" class="ux-dialog ux-dialog-scroll" aria-labelledby="twSlotTitle" aria-describedby="twSlotSub">
 <form method="dialog" id="twSlotForm" novalidate>
  <div class="ux-dialog-head">…title, sub, × [data-dialog-close]…</div>
  <p id="twSlotStatus" class="ux-choice-status tw-dialog-status" role="status" aria-live="polite"></p>
  <div class="ux-dialog-body" id="twSlotBody" tabindex="-1">
    <p id="twSlotDraft" class="ux-field-hint" hidden>…draft words + <button type="button" id="twSlotFromSaved">…</button></p>
    <section id="twSlotEdit"> <fieldset class="ux-field">Lectures: <table class="ux-rows">…</table> Add</fieldset> <fieldset class="ux-field">Labs …</fieldset></section>
    <section id="twSlotReview" hidden tabindex="-1"><h3 id="twSlotReviewTitle">…</h3> confirmationHtml(parked)</section>
  </div>
  <p class="tw-dialog-alert" id="twSlotAlert" role="alert" hidden></p>
  <div class="ux-dialog-foot"><p class="ux-dialog-foot-note" id="twSlotNote"></p>
   <button type="button" class="btn btn-sm btn-neutral" data-dialog-close>Cancel</button>
   <button type="button" class="btn btn-sm btn-neutral" id="twSlotBack" hidden>Back to editing</button>
   <button type="submit" class="btn btn-sm btn-primary" id="twSlotSave" data-tw-mutation aria-describedby="twSlotNote">Save</button></div>
 </form></dialog>
```

A row (`<tr data-row-key>`):
- `<th scope="row" id>` with the column number, plus a visually hidden "Lecture column" / "Lab column";
- the label: `input[type=text][dir=auto]`;
- start and end: `input.ux-rows-time[type=text][inputmode=numeric][dir=ltr][autocomplete=off][spellcheck=false]`;
- every input `aria-labelledby` = its column header plus the row's th;
- a length cell; flag pills; Up, Down and Remove buttons (`type=button`, named by the label);
- a hidden `p.ux-field-error` in a full-width cell, which the inputs reference with `aria-describedby`.

##### Behaviour

**Open** (`#twSlotEdit`, TPL:70):
- No scenario, or published: the toast as today.
- Otherwise `UXControls.dialog.open(dlg, {initialFocus: first label, returnFocus: () => $('twMoreMenu')})`.
- Rows come from the effective columns, or from this scenario's draft with `#twSlotDraft` shown.

**Edit:**
- Add appends a row and focuses its label.
- Remove deletes the row and focuses the next row's label, or Add. **It is disabled (`aria-disabled`, with its reason) on a list's last row.**
- Up and Down swap rows and keep focus on the same button.
- The length updates on input.
- Every change updates the draft.

**Save** (submit; `preventDefault` as in 5c):
1. Clear the marks. The body becomes `inert` and the row order is frozen as `sent = [rowKeys]`.
2. `SC.save` with:
   - `send`: `dialogWrite` → `{data}` | `{error}` (the two codes) | `{data: null}`, with the failure in `#twSlotAlert` (REFUSAL words; otherwise "The columns were not saved.");
   - `invalid`: `aria-invalid` plus the row's error (`FIELD`, `problemText`) on `sent[index]` of that list; focus the first one; `index −1` goes to the alert;
   - `confirm`: show the review (the parked lead), show Back, relabel Save "Save anyway", focus the review heading, resolve `true` on submit and `false` on Back **or on dialog close**. Back restores the rows and focuses Save;
   - `report`: write the alert.
3. Lift `inert`.
4. On data:
   - clear the draft;
   - `UXControls.dialog.close`;
   - `TW.scenario = {...TW.scenario, ...data.scenario}`;
   - `await selectBoard(TW.boardId)`;
   - a success toast, or a warning naming the parked meetings when `hidden_count > 0` (through `dialogSay` if a dialog is open).

**Holds:** Save is `data-tw-mutation`; the note gives the first hold. Escape during a write follows §7.3 D17a.

**Cancel or Escape:** closes and keeps the draft (E15d).

**Removed:** `openSlotEditor` (PAGE:5818-5889).

##### States

| State | Dialog |
|---|---|
| Published, or no scenario | Not opened; toast |
| A job or a write in flight | Opens; Save held with its reason |
| Server invalid | Fields marked on the rows sent; alert; edits kept |
| 409 | Review step; Back or Save anyway |
| The list changed while reviewing | The new list; after 4 attempts, the module's words |
| 503 or network | Alert; edits kept |
| Closed unsaved | Draft kept; offered on the next open |
| Saved | Closes; board read again; toast |

##### Strings

| Where | English | Arabic |
|---|---|---|
| Title, sub | Edit time slots · The grid's columns, for every board of this scenario · {scenario} | تعديل الفترات الزمنية · أعمدة الجدول لكل لوحات هذا السيناريو · {scenario} |
| Lists | Lectures (75 min) · Labs (100 min) | المحاضرات (75 دقيقة) · المعامل (100 دقيقة) |
| Headers | Label · Start · End · Length · Rule | الاسم · البداية · النهاية · المدة · القيد |
| Length | {n} min / — | pluralAr {one:'دقيقة واحدة', two:'دقيقتان', few:'{n} دقائق', many:'{n} دقيقةً', other:'{n} دقيقة'} |
| Flags | Online only · Policy column | عن بُعد فقط · عمود سياسة |
| Row actions | Move {label} up · Move {label} down · Remove {label} | نقل {label} إلى الأعلى · نقل {label} إلى الأسفل · حذف {label} |
| Last row | A list keeps at least one column: an empty list would mean the default columns. | تبقى في القائمة فترة واحدة على الأقل: القائمة الفارغة تعني الأعمدة الافتراضية. |
| Add | Add a lecture column · Add a lab column | إضافة عمود محاضرات · إضافة عمود معامل |
| Draft | Your unsaved columns are back. · Start from the saved columns | عادت أعمدتك غير المحفوظة. · البدء من الأعمدة المحفوظة |
| Review | Check before saving · Back to editing · Save anyway | راجِع قبل الحفظ · العودة إلى التعديل · حفظ على أي حال |
| Progress, failure | Saving the columns… · The columns were not saved. | جارٍ حفظ الأعمدة… · لم تُحفظ الأعمدة. |
| Field error | {field}: {problem} | {field}: {problem} |
| Toasts | Columns saved: {n} lecture, {m} lab. · …; now under their tables, outside the columns: {meetings}. | حُفظت الأعمدة: المحاضرات {n}، المعامل {m}. · …؛ أسفل جداولها الآن خارج الأعمدة: {meetings}. |

##### Tests

Module (`timetable-slot-config.test.cjs`):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| SC1 | `invalid` is called once, then `report`; without it, the old answer. `rowsToPayload` drops flags and keeps ''. | `invalid` ignored; flags sent |

Page (the tests at TWT:740-817 are replaced, a spec change):

| Test | What it checks | Mutants it must kill |
|---|---|---|
| SD1 | One row per effective column, flags as pills, focus on the first label; a second open is refused. | Page defaults |
| SD2 | The POST body deep-equals the rows in order, with no flag keys, and '' sent as ''. | Flags sent; a client time check |
| SD3 | A 400 on `{lab_slot_config, 1, end}` after the user reordered rows **before** Save: the mark lands on the row that was sent second; focus is there; the alert is set; no toast. | Index applied to the live order |
| SD4 | 409: the review names the meeting (bdi) with the parked lead; Save anyway sends `confirm`; Back sends nothing and restores the edits; Escape during review resolves `confirm` false (the `save()` promise settles). | Stacked dialog; a hanging promise |
| SD5 | Success with `hidden_count: 2`: closes; `/boards/2/` and `quality/` are read; a warning toast. | No `selectBoard` |
| SD6 | Add, Remove, Up, Down and the focus rules; Remove on the last row is disabled with its reason. | An empty list sent |
| SD7 | Cancel after edits, then reopen: the draft and the hint are back; "Start from the saved columns" restores the saved columns. | Edits dropped |
| SD8 | Holds (busy, job, published); rows are `inert` during the write. | Edits during the write |
| SD9 | Arabic: times `dir=ltr`, the label `dir=auto`, `pluralAr` lengths, review times isolated, "حفظ على أي حال". | No ltr; `plural()` |

Chrome (`SlotDialogTests`):
- SDB1, at 1366×650 and 1440×790, light and dark, en and ar:
  - the foot stays visible while the body scrolls;
  - "08:00" is not clipped in the 76 px input with the wide font;
  - the error and the invalid edge are ≥ 3:1;
  - no text under 11 px.
- SDB2, keyboard only: an invalid end is marked and focused; fixing it saves to the database; focus returns to `#twMoreMenu`.
- SDB3: remove the 16:00 column holding an ORM meeting; the review; Save anyway; the meeting is in the strip.

##### Risks

1. Bulk entry is slower than a textarea. Columns change rarely, and split view keeps its textarea until F13.
2. Labels are cut to 40 characters silently (SLOTS:151); the re-read shows the saved label.
3. Order is data (PAGE:2380-2386); Up and Down keep that power.

---

#### Follow-ups (recorded, not in PR 6)

- **F8.** Split view's chips and badges on `programme_view` and `seating` (`/plan-lens/` gains `programme_view` then).
- **F9.** Repair's plan scope on roles, with an oracle.
- **F10.** Whether the export's free-text search should follow the grid's words.
- **F11.** One meeting-policy rule for the page and the server in the grid rule.
- **F12.** Move `.im-form-error`, the exam repeater and `.field-error` onto `.ux-field-error` and `.ux-rows`.
- **F13.** Split view's slot editor on the 6f dialog.
- **F14.** A 503 retry in MRI's `api`; then decide whether quality takes the lane.
- **F15.** Compress JSON answers.
- **F16.** Measure `lens=1` on the 0.5 vCPU host and on a scenario larger than 665, before any default change to E2a.
- **F17.** (Found in 6b.) With one teacher per section on, the per-plan export raises on a scenario whose sections cannot be built (QA 662 and 664: "English board copies must each use the same complete weekly pattern", from `section_teacher_texts`), so the download fails where the quality read says "unassessed". Decide the export's answer (a refusal naming the boards, or files without teachers).
- **F18.** (6b review.) The programme view reads the scenario row by primary key for the elective mappings' year and term, so PV7 is 5 + 2 per programme, not the spec's 4 + 2. Read the row where the quality view now only checks that it exists, and hand its year and term down through the lens to `programme_view`; PV7 and SQ3 then pin 4 + 2.
- **F19.** (6b review.) PV1's workbook oracle is recorded with one teacher per section and the day-off policy off; the mark flag is on only in the two `grouping/marked_flag_on` scenarios. Add a scenario recorded under all three production flags with a mark that decides a role, so the oracle covers the production path. Today that path rests on the grouping tests and the real-data runs (663, 665 and 666 under production flags: the view's roles equal the export's drawn roles).

#### Owner questions (decided by the owner)

- **Q4.** Should the slot dialog set or remove the online-only mark? **Decided by the owner:** no. The dialog shows the online-only mark read-only (E15b); the server's review names a mark a change would lose.
- **Q5.** Should the dialog offer "Use the default columns" explicitly? **Decided by the owner:** no. A list always keeps one row, and there is no "Use the default columns" button (E15c), because an empty list silently means the defaults.

**Provenance.** The measurement and roles probe is `scratchpad/pr6/critic_roles.py` (the session's scratch directory), run on the QA copy `scratchpad/pr6/critic.sqlite3`. The cost figures in F come from re-running `scratchpad/pr6/combined2.py` on that copy.
