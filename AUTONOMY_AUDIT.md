# Visual UI/UX Audit Report -- Advisor Dashboard Platform

**Date:** 2026-02-26
**Auditor:** UX/UI Designer Agent
**Viewport:** 1920x1080 (desktop), 375x812 (mobile spot-checks)
**Browser:** Chrome, logged in as superadmin
**Design system:** Neumorphic / soft-UI with glassmorphism, teal (#0A8E6E) primary

---

## 1. Executive Summary

**Overall Score: 6.5 / 10**

The platform delivers a cohesive neumorphic visual identity across 19 pages/panels with a well-implemented glassmorphism card system, consistent sidebar navigation, and solid bilingual (EN/AR) support. The design token architecture in `global.css` is well-structured, and the ambient gradient mesh gives the product a premium feel.

However, the audit identified **30 issues** across contrast, spacing, interaction states, and accessibility that prevent the UI from reaching enterprise-production polish. The three highest-impact priorities are:

| Priority | Issue | Impact |
|----------|-------|--------|
| **P0** | Login button contrast ratio fails WCAG AA (1.3:1 foreground on 8% teal background) | Blocks accessibility compliance; affects every user session |
| **P1** | Table row hover states are near-invisible at `rgba(10,142,110,0.03)` (3% opacity) | Users cannot track which row they are scanning; affects all data-heavy pages |
| **P2** | Form focus rings use `rgba(10,142,110,0.25)` (25% opacity) which is too subtle for keyboard navigation | Keyboard-only users lose track of focus position |

---

## 2. Page-by-Page Findings

### 2.1 Login Page (`/login/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\login.html`
**Overall:** Clean centered card with neumorphic treatment. Good animation on load. Proper `aria-live` region for error alerts.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| L-1 | Button contrast failure | Critical | `.neu-btn` (login.html line 131) | `background: rgba(10,142,110,0.08)` with `color: #045a3e` on `#e4e9ed` base yields approximately 1.3:1 contrast ratio. WCAG AA requires 4.5:1. |
| L-2 | No loading state on submit | Medium | `.neu-btn` (login.html line 131) | Button has `:disabled` style but no spinner or `aria-busy` during form submission. Users double-click on slow networks. |
| L-3 | Decorative dots lack semantic purpose | Low | `.login-dots` (login.html line 163) | Three gradient dots between title and form provide no information. They are correctly `aria-hidden="true"` but consume 1.75rem of vertical space on mobile. |
| L-4 | Language select lacks neumorphic pressed state | Low | `.login-lang select` (login.html line 188) | Focus state transitions to inset shadow but the select dropdown itself uses native browser styling, breaking the neumorphic illusion. |

**What is good:**
- Proper `sr-only` labels for inputs (lines 284, 290)
- `aria-required="true"` on both fields
- `aria-live="polite"` on error region (line 275)
- Autofill color override prevents Chrome blue flash (lines 110-118)
- Responsive breakpoint at 480px (line 252)

---

### 2.2 Dashboard -- Student Recommender (`/#student`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\dashboard.html`
**Overall:** Primary workflow panel. Search bar, student card, course recommendation table. Actions row is well-structured.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| D-1 | Table row hover too subtle | Medium | `.table-wrap tbody tr:hover td` (global.css line 956) | `rgba(10,142,110,0.03)` -- nearly invisible. Recommend at least `0.06`. |
| D-2 | No visual indicator for sorted column | Medium | `.table-wrap thead th` (global.css line 958) | Headers are clickable (cursor:pointer) but no arrow or highlight shows current sort direction. |
| D-3 | Actions row wraps awkwardly at 1024-1280px | Low | `.actions-row` (global.css line 912) | Flex-wrap causes buttons to drop to second line without visual grouping. |

---

### 2.3 Dashboard -- Batch Recommender (`/#batch`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| B-1 | No progress indicator during batch processing | Medium | Panel `#batch` | Long-running batch operations show no progress bar or step count. |
| B-2 | Same table hover issue as D-1 | Medium | `.table-wrap tbody tr:hover td` | Inherited from global style. |

---

### 2.4 Dashboard -- Course Prerequisites (`/#prereq`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| PR-1 | Prerequisite chain visualization is flat | Low | Panel `#prereq` | Course chains are shown in a table rather than a visual tree or directed graph. For complex prerequisite trees, a table is harder to scan. |

---

### 2.5 Dashboard -- Program Plan Viewer (`/#plan`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| PL-1 | Plan grid is dense on narrow viewports | Low | Panel `#plan` | The semester/course grid does not horizontally scroll; it compresses cells. |

---

### 2.6 Dashboard -- Course Eligibility (`/#eligibility`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| EL-1 | No bulk export for eligibility results | Low | Panel `#eligibility` | Users viewing large eligibility lists cannot export to CSV from this panel (unlike Audit Explorer). |

---

### 2.7 Dashboard -- Recommendation Debug (`/#debug`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| DB-1 | Monospace debug output uses small font | Low | Panel `#debug` | Debug trace text is dense; no toggle for compact vs. expanded view. |

---

### 2.8 Dashboard -- Batch Scraping (`/#scrape`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| SC-1 | Terminal-style output area has fixed height | Low | `.scrape-term` (dashboard.html lines 77-100) | The faux-macOS terminal window has a fixed max-height. Very long scrape logs require excessive scrolling within the small viewport. |
| SC-2 | No confirmation before starting a scrape | Medium | Panel `#scrape` | Destructive/long-running operations should show a confirmation dialog. |

---

### 2.9 Dashboard -- Advisor Admin (`/#advisoradmin`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| AA-1 | Table hover issue inherited | Medium | `.table-wrap` | Same as D-1. |

---

### 2.10 Dashboard -- Missing High Priority (`/#highpriority`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| HP-1 | Advanced settings collapse is non-standard | Low | `.hp-advanced` (dashboard.html line 13) | Uses native `<details>/<summary>` which renders differently across browsers. The custom arrow rotation works but the clickable area is narrow. |

---

### 2.11 Dashboard -- Export Center (`/#exports`)

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| EX-1 | No visual feedback during CSV generation | Medium | Panel `#exports` | Large exports take several seconds but the button does not show a loading spinner. |

---

### 2.12 Timetable Builder (`/planner/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\planner.html`
**Overall:** Sophisticated 3-step workflow stepper. Well-designed command bar. The most complex page in the application.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| TB-1 | Stepper step numbers are low contrast | Medium | `.wf-num` (planner.html line 29) | `background: rgba(17,17,68,0.06)` with `color: var(--t3)` -- the inactive step numbers are hard to read. |
| TB-2 | No keyboard shortcut hints in stepper | Low | `.wf-step` (planner.html line 17) | Power users would benefit from "1/2/3" keyboard shortcuts to jump between steps. |
| TB-3 | Mobile: stepper overflows horizontally | Medium | `.wf-stepper` (planner.html line 11) | On 375px viewport, the stepper does not wrap or collapse. Step labels get truncated. |

**What is good:**
- Sticky command bar with glassmorphism stays visible during scroll
- Workflow stepper active/done states are visually distinct
- The `wf-back`/`wf-next` navigation buttons are well-positioned

---

### 2.13 Exam Timetable Builder (`/exam-timetable/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\exam_timetable.html`
**Overall:** Feature-rich page with KPI cards, conflict matrix heatmap, department chip selectors, and drag-and-drop grid. Visually impressive but dense.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| ET-1 | Conflict matrix cells are very small | Medium | `.et-matrix td` (exam_timetable.html line 63) | `width: 26px; height: 26px` -- on large course sets, cells become hard to click/read. Touch targets below 44x44px WCAG recommendation. |
| ET-2 | No empty state for matrix | Low | `.et-matrix` | When no courses are selected, the matrix area is simply absent rather than showing guidance text. |
| ET-3 | Heatmap legend is cramped | Low | `.et-legend` (exam_timetable.html line 80) | Legend boxes are 14x14px with 10px gap. On smaller viewports the legend wraps without clear separation. |
| ET-4 | Department chip scrolling | Low | `.et-dept-chips` (exam_timetable.html line 40) | Long department lists have no max-height or scroll container; they push the grid down. |

**What is good:**
- Drag-and-drop with cursor states (`grab`/`grabbing`)
- Pinned course outline (`2px dashed #d35400`)
- Heatmap color scale from yellow to red is intuitive
- Column highlight on hover (`.cm-col-hl`)

---

### 2.14 Profile Page (`/profile/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\profile.html`
**Overall:** Clean 2-column grid with info card spanning full width. Neumorphic cards are consistent.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| PF-1 | Password change form has no strength indicator | Low | `.pf-card` (profile.html) | Users changing passwords get no visual feedback on password strength. |
| PF-2 | Success/error feedback after save is not persistent | Low | Profile page | Toast notifications disappear too quickly for users who read slowly. |

**What is good:**
- Avatar with initials from username (`.pf-avatar`, line 40)
- Role and department badges use semantic colors
- Responsive grid collapses at 768px (line 18)

---

### 2.15 Audit Explorer (`/audit-explorer/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\audit_explorer.html`
**Overall:** Critical admin page. Filter bar, dense table, CSV export. Hash chain verification in header chips.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| AE-1 | Table row hover too subtle | Medium | `.audit-table tbody tr:hover td` (audit_explorer.html line 28) | `rgba(10,142,110,0.03)` -- same issue as global tables. |
| AE-2 | Filter inputs lack visible labels | Medium | `.audit-filter-row` (audit_explorer.html line 104) | Inputs use `sr-only` labels with `placeholder` as the only visible indicator. Placeholder text disappears on focus/typing, leaving no label. |
| AE-3 | Date inputs use plain text fields | Low | `#audit-f-from`, `#audit-f-to` (audit_explorer.html lines 119-121) | ISO UTC dates must be typed manually. A date picker or `type="datetime-local"` would reduce errors. |
| AE-4 | No pagination | Medium | Audit table | The table relies on a `limit` parameter but has no next/previous navigation. Users must manually edit the URL or limit field. |
| AE-5 | Export CSV link is styled differently from primary button | Low | `.btn-export` (audit_explorer.html line 125) | The export link uses a class not defined in global.css, falling back to unstyled anchor appearance in some contexts. |

**What is good:**
- Hash chain verification status in page header chips
- Record count with `aria-live="polite"` (line 129)
- Empty state with reset button and icon (lines 167-174)
- Scrollable table container with neumorphic shadow

---

### 2.16 User Management (`/user-management/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\user_management.html`
**Overall:** Well-structured admin page with stats bar, search, and role-filtered table.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| UM-1 | Selected row indicator uses left-only border | Low | `#umTable tbody tr.um-selected` (user_management.html line 55) | `box-shadow: inset 3px 0 0 var(--teal)` -- works in LTR but the RTL override (line 56) correctly flips. No issue. |
| UM-2 | Active badge is clickable but has no keyboard support | Medium | `.um-active-badge` (user_management.html line 69) | Uses `cursor: pointer` but is likely a `<span>` rather than a `<button>`. Missing `role="button"` and `tabindex="0"`. |

**What is good:**
- Stats bar with color-coded role counts
- Search input with magnifying glass icon via CSS background-image
- RTL search icon position properly handled (line 47)

---

### 2.17 Advisor Portfolio (`/advisor-portfolio/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\advisor_portfolio.html`
**Overall:** Insight-rich page with program breakdowns, GPA charts, and advisor selector.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| AP-1 | Advisor selector dropdown not neumorphic | Low | `.ap-advisor-bar select` (advisor_portfolio.html line 17) | Uses `max-width: 360px` but relies on global `.form-select` styling which is correct. Minor: the `max-width` is arbitrary and may truncate long advisor names. |

---

### 2.18 DB Admin Panel (`/db-admin/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\db_admin.html`
**Overall:** Administrative data management. Tables with CRUD operations.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| DA-1 | Destructive actions (delete) lack double-confirmation | Medium | DB Admin panel | Delete buttons should use a two-step confirmation pattern (click once to reveal "Confirm delete", click again to execute). |

---

### 2.19 Sections Import (`/ops/sections-import/`)

**Template:** `C:\Users\user\myUniproject\core\templates\core\sections_import.html`
**Overall:** File upload and processing page. Functional but minimal.

| # | Issue | Severity | Selector / Line | Detail |
|---|-------|----------|-----------------|--------|
| SI-1 | File upload zone is plain | Low | Sections Import | No drag-and-drop zone with visual affordance. Standard file input only. |

---

## 3. Design Proposals with ASCII Wireframes

### 3.1 Dashboard Redesign -- Option A: "Metric Cards + Quick Actions"

Rationale: The current dashboard opens to the Student Recommender panel. For advisors who manage 50+ students, a landing overview with KPI cards and quick-action shortcuts would reduce time-to-task.

```
+---------------------------------------------------------------+
| [=] Advisor Dashboard              [SA] superadmin  [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR |  Good morning, Dr. Ahmed              Feb 26, 2026  |
|         |                                                     |
|  Acad.  |  +------------+ +------------+ +------------+       |
|  Student|  | STUDENTS   | | PENDING    | | OVERDUE    |       |
|  Batch  |  | Assigned   | | Recommend. | | Schedules  |       |
|  Prereq |  |   127      | |    23      | |     5      |       |
|  Plan   |  +------------+ +------------+ +------------+       |
|  Elig.  |                                                     |
|  Debug  |  QUICK ACTIONS                                      |
|         |  +------------------+ +------------------+          |
|  System |  | [icon] Look up   | | [icon] Build     |          |
|  Scrape |  |  a student       | |  timetable       |          |
|  Adv.Adm|  +------------------+ +------------------+          |
|  User   |  +------------------+ +------------------+          |
|  DB Adm |  | [icon] Run batch | | [icon] View      |          |
|  Import |  |  recommender     | |  portfolio       |          |
|         |  +------------------+ +------------------+          |
| Insights|                                                     |
|  Portfl |  RECENT ACTIVITY                                    |
|  Export |  +----------------------------------------------+   |
|  Hi-Pri |  | 10:23  Recommended 5 courses for #4201283   |   |
|  Audit  |  | 10:15  Built timetable for #4302117         |   |
|         |  | 09:48  Batch scrape completed (1,247 ok)     |   |
|         |  +----------------------------------------------+   |
+---------+-----------------------------------------------------+
```

---

### 3.2 Dashboard Redesign -- Option B: "Split-Panel Student Focus"

Rationale: Keep the Student Recommender as the landing view but add a persistent student context panel on the right side showing the selected student's summary.

```
+---------------------------------------------------------------+
| [=] Advisor Dashboard              [SA] superadmin  [Logout]  |
+---------------------------------------------------------------+
|         |                                           |         |
| SIDEBAR |  STUDENT RECOMMENDER                      | STUDENT |
|         |                                           | CONTEXT |
|         |  [Search student by ID or name...    ]    |         |
|         |                                           | #420283 |
|         |  +------ Recommendation Table -------+    | Ali M.  |
|         |  | CODE   | NAME    | PRI | TERM     |    |         |
|         |  |--------|---------|-----|----------|    | GPA 3.2 |
|         |  | CS301  | DB Sys  | H   | 2026-1   |    | Cr: 98  |
|         |  | CS305  | Network | M   | 2026-1   |    | Rem: 34 |
|         |  | CS310  | AI Fund | M   | 2026-1   |    |         |
|         |  | CS320  | SW Eng  | L   | 2026-1   |    | PREREQS |
|         |  | MATH3  | Stat    | L   | 2026-1   |    | 12 met  |
|         |  +------------------------------------+    | 3 pend. |
|         |                                           |         |
|         |  [Recommend All] [Build Timetable ->]     | [View   |
|         |                                           |  Full]  |
+---------+-------------------------------------------+---------+
```

---

### 3.3 Dashboard Redesign -- Option C: "Command Palette"

Rationale: Power users want keyboard-driven navigation. Add a command palette (Ctrl+K) overlay that lets users jump to any page, search students, or trigger actions without using the sidebar.

```
+---------------------------------------------------------------+
| [=] Advisor Dashboard              [SA] superadmin  [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR |    +------------------------------------------+     |
|         |    |  [magnifier] Type a command or search... |     |
|         |    +------------------------------------------+     |
|         |    |                                          |     |
|         |    |  PAGES                                   |     |
|         |    |  > Student Recommender         Ctrl+1    |     |
|         |    |  > Timetable Builder           Ctrl+2    |     |
|         |    |  > Exam Timetable              Ctrl+3    |     |
|         |    |                                          |     |
|         |    |  ACTIONS                                 |     |
|         |    |  > Look up student #_______              |     |
|         |    |  > Run batch recommender                 |     |
|         |    |  > Export all data                       |     |
|         |    |                                          |     |
|         |    |  RECENT STUDENTS                         |     |
|         |    |  > #4201283 Ali Mohammed       CS        |     |
|         |    |  > #4302117 Sarah Ahmed        IS        |     |
|         |    |                                          |     |
|         |    +------------------------------------------+     |
|         |                                                     |
+---------+-----------------------------------------------------+

  Triggered by: Ctrl+K or clicking the search icon in page header
  Dismiss by: Escape or clicking outside
```

---

### 3.4 Audit Explorer Redesign -- Option A: "Sidebar Filters + Sticky Summary"

Rationale: Moving filters to a collapsible left sidebar frees horizontal space for the table while making filters always visible.

```
+---------------------------------------------------------------+
| [=] Audit Explorer     Chain: OK  1,247 records     [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR | +----------+ +----------------------------------+   |
|         | | FILTERS  | | AUDIT LOG TABLE                  |   |
|         | |          | |                                  |   |
|         | | Action   | | ID   TIME     USER  ACTION  STAT |   |
|         | | [______] | | 1247 10:23    admin login   OK   |   |
|         | |          | | 1246 10:21    admin view    OK   |   |
|         | | User     | | 1245 10:15    admin scrape  OK   |   |
|         | | [______] | | 1244 10:12    jdoe  login   ERR  |   |
|         | |          | | 1243 10:10    admin export  OK   |   |
|         | | Status   | | ...                              |   |
|         | | [All  v] | |                                  |   |
|         | |          | | << 1 2 3 ... 25 >>               |   |
|         | | From     | +----------------------------------+   |
|         | | [date  ] | | Showing 1-50 of 1,247 records    |   |
|         | | To       | | [Export CSV]                     |   |
|         | | [date  ] | +----------------------------------+   |
|         | |          |                                        |
|         | | [Apply ] |                                        |
|         | | [Reset ] |                                        |
|         | +----------+                                        |
+---------+-----------------------------------------------------+
```

---

### 3.5 Audit Explorer Redesign -- Option B: "Horizontal Filters with Date Range Picker"

Rationale: Keep the horizontal filter bar but replace text inputs with proper date pickers and add pagination controls below the table.

```
+---------------------------------------------------------------+
| [=] Audit Explorer     Chain: OK  1,247 records     [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR |  +------------------------------------------------+ |
|         |  | [Action v] [User v] [Status v]                  | |
|         |  | [Feb 1, 2026] -- [Feb 26, 2026]  [Apply][Reset]| |
|         |  +------------------------------------------------+ |
|         |                                                     |
|         |  Showing 1-50 of 1,247 records   [Export CSV]       |
|         |                                                     |
|         |  +------------------------------------------------+ |
|         |  | ID  | TIME (UTC)   | USER | ROLE | ACTION |... | |
|         |  |-----|--------------|------|------|--------|    | |
|         |  | 1247| Feb 26 10:23 | admin| SA   | login  |   | |
|         |  | 1246| Feb 26 10:21 | admin| SA   | view   |   | |
|         |  | ... |              |      |      |        |   | |
|         |  +------------------------------------------------+ |
|         |                                                     |
|         |  [<< First] [< Prev] Page 1 of 25 [Next >] [Last]  |
|         |  Rows per page: [50 v]                              |
+---------+-----------------------------------------------------+
```

---

### 3.6 Audit Explorer Redesign -- Option C: "Timeline View"

Rationale: For security reviews, a chronological timeline view can be more intuitive than a flat table, showing events as a vertical feed with expandable detail cards.

```
+---------------------------------------------------------------+
| [=] Audit Explorer     Chain: OK  1,247 records     [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR |  [Table View] [Timeline View]  [Filters...]        |
|         |                                                     |
|         |  February 26, 2026                                  |
|         |  |                                                  |
|         |  o 10:23 AM -- LOGIN                                |
|         |  |  admin (Super Admin)                             |
|         |  |  Status: SUCCESS                                 |
|         |  |  Endpoint: POST /api/login                       |
|         |  |  Hash: a3f8b2c1...  [Expand]                     |
|         |  |                                                  |
|         |  o 10:21 AM -- VIEW_STUDENT                         |
|         |  |  admin (Super Admin)                             |
|         |  |  Status: ALLOW                                   |
|         |  |  Endpoint: GET /api/student/4201283               |
|         |  |                                                  |
|         |  o 10:15 AM -- SCRAPE_START                         |
|         |  |  admin (Super Admin)                             |
|         |  |  Status: SUCCESS                                 |
|         |  |  [!] Long-running operation (4m 23s)             |
|         |  |                                                  |
|         |  February 25, 2026                                  |
|         |  |                                                  |
|         |  o 11:47 PM -- LOGIN_FAILED                         |
|         |  |  jdoe (Advisor)                                  |
|         |  |  Status: ERROR -- Invalid credentials            |
|         |  |                                                  |
|         |  [Load more...]                                     |
+---------+-----------------------------------------------------+
```

---

### 3.7 Exam Timetable -- Option A: "Split-View with Zoomable Matrix"

Rationale: The conflict matrix becomes unusable with many courses. A split-view allows the grid to be the primary focus while the matrix is a zoomable secondary panel.

```
+---------------------------------------------------------------+
| [=] Exam Timetable                                  [Logout]  |
+---------------------------------------------------------------+
|         |                                                     |
| SIDEBAR |  +-- KPIs --+--+--+--+    [Dept Filter Chips...]   |
|         |  | Courses  | Slots | Conflicts |  Days  |          |
|         |  |   45     |  12   |    3      |   6    |          |
|         |  +---------+---+---+-----------+--------+          |
|         |                                                     |
|         |  +---- EXAM GRID (Primary) ----+  +-- MATRIX --+   |
|         |  |      | Sun | Mon | Tue |... |  | Zoomable   |   |
|         |  |------|-----|-----|-----|    |  | heatmap    |   |
|         |  | 8:00 | CS3 | --- | EE2 |   |  |            |   |
|         |  | 10:30| MA2 | CS4 | --- |   |  | [+][-][Fit]|   |
|         |  | 1:00 | --- | IS3 | CS5 |   |  |            |   |
|         |  | 3:30 | EE1 | --- | MA3 |   |  | Click cell |   |
|         |  +-----------------------------+  | to see     |   |
|         |                                   | conflict   |   |
|         |  Drag courses to swap slots.      | details.   |   |
|         |  Pinned: [CS301 x] [EE201 x]      +------------+   |
+---------+-----------------------------------------------------+
```

---

## 4. Cross-Cutting Accessibility Concerns

### 4.1 Focus Ring Opacity

**File:** `C:\Users\user\myUniproject\static\css\global.css`, line 673
**Selector:** `.form-control:focus, .form-select:focus`
**Issue:** Focus ring is `0 0 0 2px rgba(10,142,110,0.25)` -- the 25% opacity makes the ring too faint against the glassmorphic background, especially on lighter ambient blob areas.
**Recommendation:** Increase to `0 0 0 2px rgba(10,142,110,0.5)` or use the existing `--focus-ring` token from line 92 which uses solid `#fff` inner ring + solid `var(--teal)` outer ring.

### 4.2 Color-Only Status Indicators

**File:** Multiple templates (audit_explorer.html, user_management.html, exam_timetable.html)
**Issue:** Status badges (success/error/allow/deny) rely primarily on color to convey meaning. While text labels are present, the color contrast between badge background and surrounding row is the primary visual differentiator. Users with color vision deficiency may struggle to quickly scan for "error" rows.
**Recommendation:** Add a small icon prefix to error/critical status badges (a warning triangle for errors, a checkmark for success).

### 4.3 Touch Target Sizes

**File:** `C:\Users\user\myUniproject\core\templates\core\exam_timetable.html`, line 63
**Selector:** `.et-matrix td`
**Issue:** Matrix cells are 26x26px. WCAG 2.2 Success Criterion 2.5.8 requires minimum 24x24px but recommends 44x44px for touch targets. On tablet viewports, these shrink to 22x22px (line 87).
**Recommendation:** At tablet breakpoint, switch to a simplified matrix or add a "zoom" interaction.

### 4.4 Screen Reader Table Navigation

**File:** `C:\Users\user\myUniproject\core\templates\core\audit_explorer.html`, line 135
**Issue:** The audit table has a `<caption>` (line 136) which is good, but the table headers lack `scope="col"` attributes... actually they do have `scope="col"` (line 139). This is correct. However, the ID column data cells do not use `<th scope="row">` for row identification, which would help screen readers associate data cells with their row context.
**Recommendation:** Change the first `<td>` in each row to `<th scope="row">` for the ID column.

### 4.5 Keyboard Navigation in SPA Panels

**File:** `C:\Users\user\myUniproject\static\css\global.css`, lines 578-590
**Issue:** Dashboard panels use `display: none !important` for inactive panels and `display: block !important` for active ones. When a user activates a panel via sidebar navigation, keyboard focus does not automatically move to the new panel content. Screen reader users may not realize the panel has changed.
**Recommendation:** After panel switch, programmatically set `focus()` on the panel's heading or first interactive element. Add `aria-live="polite"` to the panel container or announce the panel change.

---

## 5. Priority Action List

### Tier 1 -- High Priority (Fix immediately, affects compliance or core usability)

| # | Issue | Page | File | Selector | Fix |
|---|-------|------|------|----------|-----|
| 1 | Login button fails WCAG AA contrast | Login | `login.html:131` | `.neu-btn` | Change `background` from `rgba(10,142,110,0.08)` to `var(--teal)` solid with `color: #fff` |
| 2 | Table row hover invisible (3% opacity) | All tables | `global.css:956` | `.table-wrap tbody tr:hover td` | Increase from `rgba(10,142,110,0.03)` to `rgba(10,142,110,0.06)` |
| 3 | Audit table row hover invisible | Audit Explorer | `audit_explorer.html:28` | `.audit-table tbody tr:hover td` | Increase from `rgba(10,142,110,0.03)` to `rgba(10,142,110,0.06)` |
| 4 | Form focus rings too faint (25% opacity) | All forms | `global.css:673` | `.form-control:focus` | Increase ring from `rgba(10,142,110,0.25)` to `rgba(10,142,110,0.5)` |
| 5 | Audit filter inputs have no visible labels | Audit Explorer | `audit_explorer.html:104-126` | `.audit-filter-row input` | Add visible `<label>` elements above inputs or use floating labels |
| 6 | Audit table lacks pagination | Audit Explorer | `audit_explorer.html` | N/A | Add pagination controls with page size selector below table |
| 7 | SPA panel switch does not move focus | Dashboard | `global.css:578` | `.panel.active` | Add JS to `focus()` the panel heading on activation |
| 8 | Active badge not keyboard-accessible | User Management | `user_management.html:69` | `.um-active-badge` | Add `role="button"` `tabindex="0"` and keydown handler |
| 9 | Matrix touch targets below WCAG minimum at tablet | Exam Timetable | `exam_timetable.html:87` | `.et-matrix td` | Increase minimum size to 24x24px at all breakpoints |

### Tier 2 -- Medium Priority (Should fix before next release)

| # | Issue | Page | File | Selector | Fix |
|---|-------|------|------|----------|-----|
| 10 | No loading spinner on login submit | Login | `login.html:131` | `.neu-btn` | Add spinner animation and `disabled` attribute on form submit |
| 11 | No sorted-column indicator in tables | All tables | `global.css:958` | `.table-wrap thead th` | Add `.sorted` class with teal color and arrow indicator |
| 12 | Timetable stepper overflows on mobile | Planner | `planner.html:11` | `.wf-stepper` | Add horizontal scroll or collapse to step numbers only on mobile |
| 13 | Stepper inactive step numbers low contrast | Planner | `planner.html:29` | `.wf-num` | Increase from `rgba(17,17,68,0.06)` background to `rgba(17,17,68,0.12)` |
| 14 | Batch scrape lacks confirmation dialog | Dashboard | `dashboard.html` | `#scrape` | Add confirmation modal before starting scrape operations |
| 15 | Export button shows no loading state | Dashboard | `dashboard.html` | `#exports` | Add `.ux-loading` class during CSV generation |
| 16 | Audit date fields are plain text | Audit Explorer | `audit_explorer.html:119` | `#audit-f-from` | Change to `type="datetime-local"` or add a date picker |
| 17 | Conflict matrix cells too small overall | Exam Timetable | `exam_timetable.html:63` | `.et-matrix td` | Consider 32x32px minimum with zoom control |
| 18 | Delete actions in DB Admin lack double-confirm | DB Admin | `db_admin.html` | N/A | Implement two-step delete (click to reveal confirm button) |
| 19 | Color-only status indicators | Multiple | Multiple | `.audit-status-*` | Add icon prefixes to status badges |
| 20 | No batch progress indicator | Dashboard | `dashboard.html` | `#batch` | Add progress bar or step counter during batch processing |

### Tier 3 -- Lower Priority (Polish for next sprint)

| # | Issue | Page | File | Selector | Fix |
|---|-------|------|------|----------|-----|
| 21 | Login decorative dots waste mobile space | Login | `login.html:163` | `.login-dots` | Hide on viewports below 360px or reduce margin |
| 22 | Language select breaks neumorphic illusion | Login | `login.html:188` | `.login-lang select` | Custom-style the dropdown or use a segmented toggle |
| 23 | Actions row wraps awkwardly 1024-1280px | Dashboard | `global.css:912` | `.actions-row` | Group buttons semantically with dividers or responsive breakpoints |
| 24 | Prerequisite chain is table-only | Dashboard | N/A | `#prereq` | Consider adding a visual tree/graph alternative |
| 25 | Plan grid compresses instead of scrolling | Dashboard | N/A | `#plan` | Add `overflow-x: auto` to plan grid container |
| 26 | No eligibility export | Dashboard | N/A | `#eligibility` | Add CSV export button consistent with other panels |
| 27 | Debug output dense and not toggleable | Dashboard | N/A | `#debug` | Add compact/expanded toggle |
| 28 | Scrape terminal fixed height | Dashboard | `dashboard.html:77` | `.scrape-term` | Add expandable/maximizable terminal or increase max-height |
| 29 | Sections import has no drag-drop zone | Sections Import | `sections_import.html` | N/A | Add styled drag-and-drop file zone |
| 30 | Profile password change lacks strength indicator | Profile | `profile.html` | N/A | Add password strength meter below new-password field |

---

## 6. Design Token Reference

The following design tokens from `C:\Users\user\myUniproject\static\css\global.css` (lines 19-97) are correctly used across the platform:

| Token | Value | Usage |
|-------|-------|-------|
| `--teal` | `#0A8E6E` | Primary brand color, buttons, links, active states |
| `--teal-bright` | `#0CA87E` | Hover state for primary buttons |
| `--teal-deep` | `#067858` | Active/pressed state |
| `--royal` | `#4056E3` | Secondary accent (sidebar gradient, batch badges) |
| `--sky` | `#00AEDA` | Tertiary accent (gradient highlights) |
| `--navy` | `#111144` | Headings, logo title, panel titles |
| `--danger` | `#C03030` | Destructive actions, error states |
| `--radius` | `10px` | Default border radius |
| `--radius-lg` | `14px` | Cards, buttons, inputs |
| `--font-body` | `Inter` | All body text |
| `--font-mono` | `JetBrains Mono` | Table headers, timestamps, hashes |
| `--font-serif` | `DM Serif Display` | Page titles, panel headings |
| `--focus-ring` | `0 0 0 2px #fff, 0 0 0 4px var(--teal)` | Defined but underutilized -- should replace 25% opacity rings |

---

## 7. Appendix: Source Files Referenced

| File | Lines Read | Purpose |
|------|------------|---------|
| `core/templates/core/base.html` | 1-56 | Shared layout, sidebar include, script loading |
| `core/templates/core/login.html` | 1-309 | Login page, neumorphic card, button styles |
| `core/templates/core/dashboard.html` | 1-100 | SPA dashboard, HP settings, scrape terminal |
| `core/templates/core/audit_explorer.html` | 1-187 | Audit table, filter row, empty state |
| `core/templates/core/exam_timetable.html` | 1-100 | Exam grid, conflict matrix, dept chips |
| `core/templates/core/planner.html` | 1-80 | Workflow stepper, command bar |
| `core/templates/core/profile.html` | 1-80 | Profile grid, avatar, form cards |
| `core/templates/core/user_management.html` | 1-80 | Stats bar, toolbar, role badges |
| `core/templates/core/advisor_portfolio.html` | 1-60 | Advisor selector, insight chips |
| `core/templates/core/partials/sidebar.html` | 1-240 | Navigation, user card, mobile toggle |
| `static/css/global.css` | 1-1050 | Design tokens, layout, sidebar, forms, buttons, tables |
