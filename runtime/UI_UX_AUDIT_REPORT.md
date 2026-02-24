# UI/UX Production Audit Report
**Advisor Portal -- Taibah University**
**Date:** 24 February 2026
**Auditor scope:** All 9 views, sidebar partial, base template, global CSS, mockup reference

---

## Executive Summary

### Overall Maturity Score: 4.8 / 10

The application demonstrates strong *visual design intent* (glassmorphism, coherent colour palette, distinctive typography) but suffers from critical accessibility failures, extreme code duplication, inconsistent component patterns, and a fragmented design system implementation. The gap between the mockup source-of-truth and the actual templates is significant. The app is not production-ready from a WCAG 2.2 AA standpoint and has maintainability debt that will compound with each new feature.

### Top 10 Issues

| # | Severity | Issue | Impact |
|---|----------|-------|--------|
| 1 | **Blocker** | No `aria-live` regions anywhere -- dynamic content (toasts, banners, loading states, table updates) is invisible to screen readers | Screen reader users cannot use the application |
| 2 | **Blocker** | No focus trapping in any modal/dialog/drawer -- keyboard users can tab behind open overlays | Keyboard-only users get lost in the UI |
| 3 | **Blocker** | No `<h1>` on any page; heading hierarchy broken (jumps to `<h4>`) | Assistive tech cannot outline page structure |
| 4 | **High** | Contrast failures: `--t5` (#a8a8be) at 2.3:1 and `--t4` (#7a7a92) at 3.5:1 used for meaningful text on light backgrounds | WCAG 1.4.3 failure -- text unreadable for low-vision users |
| 5 | **High** | ~500 lines of JS duplicated across templates (toast system x5, dialog system x3, CSRF extraction x5, sort utility x2) | Bugs fixed in one copy remain in others; maintenance nightmare |
| 6 | **High** | `dashboard.html` has hundreds of hardcoded English strings in JS with zero i18n coverage | Arabic users see a broken mixed-language UI |
| 7 | **High** | Hit targets as small as 14x14px (checkboxes) and 28x28px (action buttons) -- below WCAG 2.5.8 minimum of 24x24px | Touch/motor-impaired users cannot reliably tap small targets |
| 8 | **High** | 3 different table styling systems, 4+ button class systems, 3 CSRF patterns -- no unified component library | Visual and behavioural inconsistency across pages |
| 9 | **Medium** | No debounce on search/filter inputs -- full table re-render via `innerHTML` on every keystroke | UI jank on large datasets; wasted DOM churn |
| 10 | **Medium** | No skeleton/shimmer loading states; content jumps from empty to populated | Perceived performance suffers; layout shifts disorienting |

---

## Screen-by-Screen Findings

---

### 1. Login (`login.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Page title | `.login-title` is a `<div>`, not an `<h1>` | High | Change to `<h1 class="login-title">` |
| Error alert | `{% if error %}` block lacks `role="alert"` | High | Add `role="alert"` to `.alert-danger` |
| Username input | Has `required` but no `aria-label` or visible `<label>` linked via `for=` | Medium | Add `<label for="username">` or `aria-label` |
| Password input | Same as above | Medium | Same fix |
| Submit button | Uses custom `.login-btn` class instead of design-system button | Low | Align with `.btn .btn-p` from mockup |
| Empty state | No "forgot password" flow or link | Low | Consider adding recovery link |
| RTL | Login card layout works (centered) but no explicit RTL testing noted | Medium | Verify input text direction and label alignment |

---

### 2. Dashboard (`dashboard.html`) -- 11 panels, ~3500 lines

#### 2A. Page Header / Navigation

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| `.page-header` | Uses `<div>`, not `<header>` | Medium | Wrap in `<header>` element |
| Page heading | `.page-header-module` is a `<div>` -- no `<h1>` | Blocker | Change to `<h1>` |
| Panel headings | All use `<h4>` with no `<h1>`-`<h3>` above them | Blocker | Use `<h1>` for page, `<h2>` for panels |
| Logout button | `btn btn-sm btn-outline-secondary` -- inconsistent with mockup's button system | Low | Migrate to `.btn .btn-g` |

#### 2B. Student Recommender Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Filter inputs (`#studentIdInput`, year, semester) | Have `placeholder` text but NO `<label>` elements | High | Add `<label>` elements with `for=` attribute |
| "Recommend" / "Load plan" buttons | Bootstrap `btn-primary` / `btn-outline-primary` -- not design-system buttons | Medium | Migrate to `.btn .btn-p` / `.btn .btn-s` |
| Recommendation cards | `.rec-card` rendered in JS with hardcoded English text ("Ready", "courses") | High | Add i18n support via `IS_AR` pattern |
| CSV export link | `<a href="#" class="btn">` -- link acting as button | Medium | Change to `<button>` |
| Student Plan Matrix | `style="color:var(--t4)"` on labels -- 3.5:1 contrast | High | Use `--t3` (#5c5c7a, 4.9:1) minimum |
| Legend labels | `style="color:var(--t5)"` -- 2.3:1 contrast | Blocker | Use `--t3` minimum |
| Term distribution progress bars | Have `role="progressbar"` + `aria-valuemin/max/now` -- good | -- | Keep |
| Course row buttons | JS-generated with hardcoded English ("Focus next", "Build plan") | High | i18n |
| `<details>` filter toggles | Proper semantic HTML -- good | -- | Keep |

#### 2C. Batch Recommender Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Batch inputs | `#batchYear`, `#batchSemester`, etc. have NO labels at all | High | Add `<label>` elements |
| Batch table | `.tbl-card` custom class, no pagination | Medium | Add pagination for large datasets |
| "Save preset" / "Load preset" buttons | Hardcoded English | High | i18n |
| Top Recommendations live badge | Hardcoded "LIVE" text | Medium | i18n |
| CSV/Summary buttons | `<a href="#">` -- links acting as buttons | Medium | Change to `<button>` |

#### 2D. Course Prerequisites Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| `#preProgram` | Has visible label, but `<label>` lacks `for=` attribute | Medium | Add `for="preProgram"` |
| `#preCourse` | No label at all | High | Add `<label>` |
| Dependency graph | "No graph data" empty state exists -- good | -- | Keep |
| Prereq links table | Empty state exists -- good | -- | Keep |

#### 2E. Program Plan Viewer Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| All heading text | Hardcoded English ("Program Plan Viewer", "Filters", "Results") | High | i18n |
| Filter dropdowns | No keyboard activation -- need Enter/Space support | Medium | Add keydown handler |
| Table headers | Have `data-sort` but `<th>` elements lack `scope="col"` | Medium | Add `scope="col"` |

#### 2F. Batch Scraping Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| `.scrape-term` terminal | Good visual design matching mockup `.term` pattern | -- | Keep |
| Scrape form inputs (`#scrapeConcurrency`, `#scrapeCsv`) | No labels | High | Add `<label>` elements |
| Refresh button | `btn-circle` icon-only -- no `aria-label` | High | Add `aria-label="Refresh scrape status"` |
| Timestamp displays | `color:var(--t5)` -- 2.3:1 contrast failure | High | Use `--t3` or `--t4` |
| "No scrape history" text | `color:var(--t5)` on empty state hint | High | Use `--t3` |

#### 2G. Recommendation Debug Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| All UI text | Hardcoded English, zero i18n | High | Full i18n pass needed |
| Debug form inputs | No labels | High | Add `<label>` elements |
| `.trace-btn` buttons | 28x28px estimated -- below 44px target | Medium | Increase to 32x32px minimum with spacing |

#### 2H. Eligibility Panel

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Static modals | `#eligibilityDetailsModal`, `#debugTraceModal` -- no focus trap | Blocker | Add focus trapping |
| Modal close buttons | Have `aria-label="Close"` -- good | -- | Keep |
| All text | Hardcoded English | High | i18n |

#### 2I. Toast System (Dashboard variant)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Toast container | No `aria-live="polite"` | Blocker | Add `aria-live="polite" role="status"` |
| Toast icons | Uses text characters (`\u2713`, `\u2715`) while other pages use SVGs | Medium | Align to SVG pattern |
| Legacy `toast()` wrapper | Used in ~30 places, wraps `notify.success/error` | Low | Refactor to use `notify` directly |

#### 2J. Command Palette (Cmd+K)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| `aria-label="Quick search"` | Present -- good | -- | Keep |
| Arrow key navigation | Implemented -- good | -- | Keep |
| Escape to close | Implemented -- good | -- | Keep |
| Focus management | Focuses input on open -- good | -- | But add focus trap |
| i18n | Command labels are hardcoded English | Medium | i18n |

---

### 3. Planner (`planner.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Workflow stepper (`.wf-step`) | Buttons (focusable) but no arrow-key navigation | Low | Add arrow-key nav for toolbar pattern |
| Inactive step labels | `color: var(--t4)` -- 3.5:1 contrast | High | Darken to `--t3` |
| Status dot `.cb-dot` | `background: var(--t5)` -- decorative, acceptable | -- | OK |
| Filter inputs (`#studentId`, `#year`, `#term`, `#mode`) | Placeholder-only, no `<label>` elements | High | Add labels |
| Status banner `#statusBanner` | Dynamically updated via `setBanner()` -- no `aria-live` | Blocker | Add `aria-live="polite"` |
| Custom toggle switch | Checkbox `width: 14px; height: 14px` -- 14x14px hit target | Blocker | Use `.form-check` wrapper with 44px clickable label area |
| `.plan-chip` palette elements | `<span>` elements, not focusable via keyboard | High | Change to `<button>` elements |
| Recommendation table `#recTable` | No empty state message when empty | Medium | Add "No recommendations yet" message |
| Shortlist `#shortlist` | No empty state message | Medium | Add empty state |
| Visual timetable `#visualGrid` | No empty state | Medium | Add empty state |
| Section picker `#sectionsPicker` | No empty state | Medium | Add empty state |
| `.planner-banner-*` banners | Good visual design with SVG icons -- well implemented | -- | Keep |
| `.pl-btn-*` buttons | Good custom button system -- consistent within planner | -- | But inconsistent with dashboard's Bootstrap buttons |
| Toast container | No `aria-live` | Blocker | Fix globally |
| i18n | Good coverage -- uses `IS_AR` pattern consistently | -- | Keep |

---

### 4. Sections Import (`sections_import.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| File input `#oracleFile` | Has `accept=` but no `required` attribute | Low | JS validates; acceptable |
| Preview table filter inputs (`#fCode`, `#fSection`, `#fDay`) | Placeholder-only, no `<label>` elements | High | Add labels |
| Filter inputs | No debounce -- fires on every keystroke | Medium | Add 250ms debounce |
| "No data yet" empty state | Present and well-worded -- good | -- | Keep |
| Status banner `#status` | Uses `.alert` classes, no `role="alert"` | High | Add `role="alert"` |
| Insert confirmation | Uses native `confirm()` -- inconsistent with custom `dlg.confirm()` used everywhere else | Medium | Replace with `dlg.confirm()` |
| KPI labels `.kpi .k` | `color: var(--t4)` -- 3.5:1 contrast | High | Darken to `--t3` |
| Table headers | `color: var(--t4)` -- 3.5:1 contrast | High | Darken to `--t3` |
| "No results match filter" | Missing -- table body empties silently when filters return zero | Medium | Add filtered empty state |
| i18n | Uses `T` translation object -- good pattern | -- | Keep |
| Toast container | No `aria-live` | Blocker | Fix globally |
| Table | Uses `<table>` with `<th>` -- semantically correct | -- | Add `scope="col"` on `<th>` |

---

### 5. User Management (`user_management.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Row action buttons `.um-row-btn` | 28x28px -- below 44px target | High | Increase to 36x36px; add `aria-label` (currently has labels -- good) |
| "Loading users..." initial state | Present -- good | -- | Keep |
| Search/filter input | `oninput="filterTable()"` -- no debounce | Medium | Add 250ms debounce |
| Table sorting | `wireSortableTable()` duplicated from advisor_portfolio | Medium | Extract to shared module |
| Table | No pagination -- renders ALL users | Medium | Add pagination (50 per page) |
| i18n | **Zero Arabic translations in JS** -- all dynamic text is English-only | Blocker | Full i18n pass required |
| Dialog system | `dlg.confirm()` / `dlg.prompt()` -- no focus trap | Blocker | Add focus trapping |
| Dialog XSS risk | `body` property uses `innerHTML` with user-supplied values | High | Ensure `esc()` is applied consistently |
| Create user form | No `type="email"` on email field | Medium | Add `type="email"` |
| Password field | No `aria-describedby` linking to password requirements | Medium | Add helper text + association |
| Generate password button `.um-gen-pw` | Small, no explicit sizing | Medium | Ensure 44px touch target |
| Toast container | No `aria-live` | Blocker | Fix globally |

---

### 6. Advisor Portfolio (`advisor_portfolio.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Search input | `oninput="apFilter()"` -- no debounce | Medium | Add 250ms debounce |
| Drawer `.ap-drawer` | No focus trap | Blocker | Add focus trapping |
| Drawer close button | 32x32px -- below 44px | Medium | Increase to 36x36px |
| Expand/collapse buttons `.ap-expand` | Have `aria-expanded` -- good | -- | Keep |
| Pagination | Implemented with 50-per-page -- good | -- | Keep |
| GPA bar labels | `.ap-gpa-labels` at `0.58rem` (~9px) with `color: var(--muted-light)` | High | Text too small and low contrast; increase to 10px and darken colour |
| i18n | Mostly English-only in JS-generated content | High | i18n pass needed |
| Filter chips `.fb-dd` | No keyboard activation handler | Medium | Add Enter/Space keydown handler |
| `.ap-hp-btn` action button | Small inline button with no sizing constraint | Medium | Ensure 44px target |
| Toast container | No `aria-live` | Blocker | Fix globally |

---

### 7. Audit Explorer (`audit_explorer.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Filter form | Uses `<form method="get">` -- proper semantic HTML | -- | Keep |
| `type="number" min="1" max="5000"` on limit | Good validation | -- | Keep |
| Table headers | `color: var(--t4)` -- 3.5:1 contrast | High | Darken to `--t3` |
| Table | Uses `<table>` -- semantically correct | -- | Add `scope="col"` on `<th>` |
| Empty state | "No audit records found" present -- good | -- | Keep |
| Table sorting | Not implemented | Low | Consider adding client-side sort |
| `.audit-table` styling | Unique class not shared with other tables | Medium | Align with design system table pattern |
| i18n | Template uses `{% trans %}` and `{% if LANGUAGE_CODE %}` -- good | -- | Keep |

---

### 8. DB Admin (`db_admin.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| Nav items `.dba-nav-item` | Proper `<button type="button">` -- good semantic HTML | -- | Keep |
| Nav `<nav>` | Has `aria-label="DB Admin operations"` -- good | -- | Keep |
| `callJson()` helper | Best fetch pattern in codebase -- handles loading, errors, CSRF | -- | Extract as shared utility |
| Delete Students inputs | `#sProgram`, `#sSection` have `<label>` but no `for=` attribute linking | Medium | Add `for=` attribute |
| Output blocks (`<pre>`) | Updated dynamically -- no `aria-live` | High | Add `aria-live="polite"` |
| `.has-error` output styling | Red text on dark background -- check contrast | Medium | Verify `#fca5a5` on `#111144` (passes) |
| Disabled states | `#tImport` disabled until preview -- good | -- | Add tooltip explaining "Preview required" |
| Dialog system | `dlg.confirm()` only -- no focus trap | Blocker | Add focus trapping |
| i18n | Good coverage with `IS_AR` pattern | -- | Keep |
| Term sections import | `<table>` elements with proper structure | -- | Add `scope="col"` |
| Toast container | No `aria-live` | Blocker | Fix globally |
| Risk badges `.dba-risk-badge` | Very small (`0.58rem`, `padding: 0.15rem 0.4rem`) -- not interactive, decorative only | Low | Acceptable for badges |

---

### 9. Sidebar (`partials/sidebar.html`)

| Component | Issue | Severity | Recommendation |
|-----------|-------|----------|----------------|
| `<aside>` element | Proper semantic -- good | -- | Keep |
| Hamburger toggle | Has `aria-label` and `aria-expanded` -- good | -- | Keep |
| Escape to close | Implemented -- good | -- | Keep |
| Backdrop click to close | Implemented -- good | -- | Keep |
| Nav SVG icons | No `aria-hidden="true"` -- screen readers try to parse inline SVGs | Medium | Add `aria-hidden="true"` to all `<span class="i">` |
| Nav links | `<a>` elements -- semantically correct for navigation | -- | Keep |
| Language switcher | `onchange="this.form.submit()"` -- works but no loading indicator | Low | Add loading state |
| User card | Uses `<div>` -- should be a `<footer>` or wrapped in `<footer>` | Low | Wrap in `<footer>` |
| Nav section labels `.nav-lbl` | `font-size: 9px` with `color: var(--t5)` at 2.3:1 contrast | High | Darken to `--t4` minimum (still fails for small text); recommend `--t3` |
| Active state `.nav-link.active` | Blue accent visible against glass -- adequate contrast | -- | Keep |

---

## Component Inventory

### Detected Components & Reuse Analysis

| Component | Files Used | Pattern(s) | Consistency |
|-----------|-----------|------------|-------------|
| **Toast/Notify** | All 6 JS-heavy pages | 2 variants (SVG vs text icons) | Poor -- 5 copies |
| **Dialog/Modal** | dashboard, user_mgmt, db_admin | 3 copies of dlg IIFE + 1 native `confirm()` | Poor -- 4 patterns |
| **Table** | All except login, planner | `table table-sm`, `tbl-card`, `audit-table` | Poor -- 3 systems |
| **Buttons** | All pages | Bootstrap (`btn-primary/outline/danger/warning`) + custom (`pl-btn`, `login-btn`, `um-row-btn`, `fb-dd`, `btn-circle`, `trace-btn`, `dba-nav-item`) | Very poor -- 10+ patterns |
| **Filter bar** | dashboard, audit, advisor, sections | `.filter-bar` / `.fb-search` / `.fb-dd` (partial) vs inline forms | Medium |
| **Page header** | All pages (except login) | `.page-header` with `.ph-chip` system | Good -- consistent |
| **Sidebar** | All pages (except login) | Single partial template | Good |
| **Panel switching** | dashboard (`.panel`), db_admin (`.dba-panel`), planner (`.wf-step` workflow) | 3 patterns | Poor |
| **Status pills/badges** | dashboard, planner, advisor | `.ph-chip`, `.pill`, `.planner-banner`, `.nav-badge` | Medium -- several variants |
| **Loading bar** | sections, db_admin, planner, dashboard | `shared-ux-js` MutationObserver pattern | Medium -- 4 copies |
| **CSRF extraction** | All 6 JS pages | 2 implementations | Poor -- 5 copies |
| **`esc()` HTML escaper** | user_mgmt, advisor | Identical function | Poor -- 2 copies |
| **Sort utility** | user_mgmt, advisor | `wireSortableTable()` | Poor -- 2 copies, subtly different |
| **Empty states** | dashboard, sections, audit, advisor, planner, db_admin | Inconsistent styling (some SVG icons, some text-only, some `var(--t5)`) | Medium |
| **Metric cards** | dashboard only | `.mc` / `.mc-hero` | N/A -- single page |
| **Terminal** | dashboard (`.scrape-term`), db_admin (`.dba-output`) | 2 different implementations | Medium |
| **Pagination** | advisor only | Custom `.pg-bar` / `.pg-btn` | N/A -- single page |

---

## Accessibility Audit Summary

### WCAG 2.2 AA Compliance Failures

| Criterion | Status | Details |
|-----------|--------|---------|
| **1.1.1 Non-text Content** | FAIL | SVG icons have no text alternatives; icon-only buttons lack `aria-label` |
| **1.3.1 Info and Relationships** | FAIL | Form inputs missing labels; heading hierarchy broken; tables missing `scope` |
| **1.3.2 Meaningful Sequence** | PASS | DOM order matches visual order |
| **1.4.1 Use of Colour** | PARTIAL | Status pills use colour + text (good); GPA uses colour alone (needs icon/text supplement) |
| **1.4.3 Contrast (Minimum)** | FAIL | `--t4` at 3.5:1, `--t5` at 2.3:1 used for meaningful text on light backgrounds |
| **2.1.1 Keyboard** | PARTIAL | Main nav works; dialogs lack focus trap; `.plan-chip` not focusable |
| **2.1.2 No Keyboard Trap** | PASS | No trapping issues (the problem is the opposite -- focus escapes dialogs) |
| **2.4.1 Bypass Blocks** | FAIL | No skip-to-content link |
| **2.4.2 Page Titled** | PASS | `<title>` blocks set via base.html |
| **2.4.3 Focus Order** | PARTIAL | Generally correct; dialog focus on open works but no trap |
| **2.4.6 Headings and Labels** | FAIL | No `<h1>`, broken hierarchy, many inputs lack labels |
| **2.5.8 Target Size** | FAIL | Checkboxes 14px, row buttons 28px, close buttons 32px |
| **3.3.1 Error Identification** | PARTIAL | Toast errors shown but no `aria-invalid` on fields |
| **3.3.2 Labels or Instructions** | FAIL | Multiple inputs have placeholder-only labels |
| **4.1.2 Name, Role, Value** | FAIL | Dynamic content lacks ARIA states; custom widgets lack roles |
| **4.1.3 Status Messages** | FAIL | No `aria-live` on any dynamically updated content |

### Quick Wins (< 1 hour each)

1. Add `aria-live="polite"` to toast containers (all 6 files)
2. Add `role="alert"` to error alert divs
3. Add `aria-hidden="true"` to decorative SVG icon `<span class="i">` wrappers
4. Add `scope="col"` to all `<th>` elements
5. Change `.page-header-module` from `<div>` to `<h1>` and panel headings to `<h2>`
6. Add skip-to-content link in `base.html`
7. Replace `--t5` text usage with `--t3` (search-and-replace in inline styles)
8. Add `aria-label` to icon-only buttons (`#scrapeRefresh`, drawer close, etc.)

---

## Design System Recommendations

### Current State vs Mockup Source-of-Truth

The mockup (`signature_6_1_taibah_refined.html`) defines a complete, coherent design system. The templates only partially implement it. Key gaps:

#### Tokens

| Token Area | Mockup | Implementation | Gap |
|-----------|--------|----------------|-----|
| Colours | Full teal/royal/sky/navy families with `--ok`, `--err`, `--warn` semantics | Partially migrated; some old blue values remain; `--success`, `--danger` tokens duplicate `--ok`, `--err` | Consolidate to mockup tokens |
| Typography | Inter body, DM Serif Display headings, JetBrains Mono code | Implemented in `global.css` | Minor -- some inline `font-family` overrides exist |
| Shadows | 5 token shadows (`--sh-sm` through `--sh-inp`) | Partially used; many inline `box-shadow` values not using tokens | Tokenise all shadows |
| Radius | `--r` (14px), `--r-s` (10px), `--r-xs` (7px), `--r-f` (9999px) | Partially used; `global.css` uses `--radius` and `--radius-sm` (different names) | Rename to match mockup |
| Easing | `--ease`, `--spring` | Not used in global.css; some pages use `cubic-bezier` directly | Add tokens to global.css |

#### Spacing Scale (Recommended)

The mockup uses an implicit 4px base scale. Formalise it:

```
--sp-0: 0
--sp-1: 4px
--sp-2: 8px
--sp-3: 12px
--sp-4: 16px
--sp-5: 20px
--sp-6: 24px
--sp-7: 28px
--sp-8: 32px
--sp-9: 36px
```

#### Typography Scale (Recommended)

Formalise from the mockup's usage:

```
--fs-xs:   9px      /* nav labels, badges */
--fs-sm:   10.5px   /* labels, captions */
--fs-base: 13px     /* body, nav items, inputs */
--fs-md:   14px     /* body default */
--fs-lg:   18px     /* section titles */
--fs-xl:   20px     /* logo */
--fs-2xl:  28px     /* metric values, page titles */
--fs-3xl:  36px     /* hero metric */
```

#### Colour Usage Rules

| Use case | Token | Do NOT use |
|----------|-------|-----------|
| Body text | `--t2` | `--t4`, `--t5` for readable text |
| Secondary text | `--t3` | `--t4` (fails WCAG on white) |
| Placeholder / decorative | `--t4` | Only on 18px+ text or non-essential |
| Disabled / ornamental only | `--t5` | Never for meaningful text |
| Primary actions | `--teal` | `--brand` (alias -- consolidate) |
| Destructive actions | `--err` / `--err-t` | `#C03030` inline |
| Warning states | `--warn` / `--warn-t` | `#9A6A08` inline |
| Success states | `--ok` | `--teal` (differentiate from primary) |

---

### Component Library Recommendations

#### Buttons -- Consolidate to 5 variants

| Variant | Class | Use case |
|---------|-------|----------|
| Primary | `.btn .btn-p` | Main CTA |
| Secondary | `.btn .btn-s` | Secondary actions |
| Ghost | `.btn .btn-g` | Tertiary / cancel |
| Danger | `.btn .btn-d` | Destructive actions |
| Circle | `.btn-circle` | Icon-only actions |

**Retire:** All Bootstrap `btn-primary`, `btn-outline-*`, `btn-success`, `btn-warning`, `btn-danger` classes.
**Retire:** `.pl-btn-*`, `.login-btn`, `.um-row-btn`, `.trace-btn`, `.fb-dd` (as button), `.ap-hp-btn` custom button classes.

#### Tables -- Consolidate to 1 pattern

Use the mockup's `.card-row` grid pattern as the standard:
- `.tbl-section` > `.tbl-header` > `.col-header` > `.card-row` rows
- Built-in sort on `.col-h` with `.sorted` state
- `.pill-g`/`.pill-r`/`.pill-a` for inline status
- `.pagination` component for paging

**Retire:** Bootstrap `table table-sm`, `.tbl-card`, `.audit-table` custom class.

#### Dialogs -- Consolidate to 1 shared module

Extract `dlg.confirm()` / `dlg.prompt()` from `dashboard.html` (the most complete version) into `static/js/dialog.js`:
- Add focus trapping (Tab wrapping between first/last focusable)
- Add focus restoration on close
- Add `aria-describedby` for dialog body content
- Replace `sections_import.html`'s native `confirm()` with `dlg.confirm()`

#### Toasts -- Consolidate to 1 shared module

Extract into `static/js/notify.js`:
- Use SVG icons (not text characters)
- Add `aria-live="polite"` to container
- Add `role="status"` to individual toasts
- Include in `base.html`

---

## Prioritised Action Plan

### Next 7 Days (Critical -- Accessibility Blockers)

| # | Task | Files | Effort |
|---|------|-------|--------|
| 1 | Add `aria-live="polite"` to all toast containers | All 6 JS pages | 30 min |
| 2 | Add focus trapping to dialog/modal/drawer system | dashboard, user_mgmt, db_admin, advisor | 2 hrs |
| 3 | Fix heading hierarchy: `<h1>` for page title, `<h2>` for sections | All 8 pages | 1 hr |
| 4 | Replace `--t5` text with `--t3` minimum for meaningful content | dashboard, planner | 30 min |
| 5 | Replace `--t4` text with `--t3` for normal-sized text | sections, audit, planner, dashboard | 30 min |
| 6 | Add `<label>` elements to ALL form inputs (or `aria-label`) | All pages with forms | 2 hrs |
| 7 | Add `aria-hidden="true"` to decorative SVG icons | All pages, sidebar | 30 min |
| 8 | Add skip-to-content link in `base.html` | base.html | 15 min |
| 9 | Increase checkbox hit targets from 14px to 44px clickable area | planner | 30 min |
| 10 | Increase `.um-row-btn` from 28px to 36px | user_management | 15 min |

### Next 30 Days (High Priority -- Consolidation & i18n)

| # | Task | Impact |
|---|------|--------|
| 11 | Extract toast system into `static/js/notify.js`, include via `base.html` | Eliminates 150 lines duplication |
| 12 | Extract dialog system into `static/js/dialog.js`, include via `base.html` | Eliminates 210 lines duplication, single place for focus-trap fix |
| 13 | Extract CSRF helper into shared module | Eliminates 5 copies |
| 14 | Extract `esc()`, `wireSortableTable()`, `q()` into shared utils | Eliminates ~70 lines duplication |
| 15 | Full i18n pass on `dashboard.html` JS (200+ hardcoded strings) | Arabic users can use dashboard |
| 16 | Full i18n pass on `user_management.html` JS | Arabic users can use user management |
| 17 | i18n pass on `advisor_portfolio.html` JS-generated content | Arabic users can use portfolio |
| 18 | Add debounce (250ms) to all search/filter `oninput` handlers | Performance improvement |
| 19 | Replace `sections_import.html` native `confirm()` with `dlg.confirm()` | Consistency |
| 20 | Add pagination to `user_management.html` table | Performance for large user lists |
| 21 | Add `role="alert"` to error messages and status banners | Screen reader error announcements |
| 22 | Add `scope="col"` to all `<th>` elements | Table accessibility |
| 23 | Add empty states to planner panels (recTable, shortlist, visualGrid, sectionsPicker) | UX completeness |
| 24 | Add "no results match filter" message to all filterable tables | UX for zero-result state |

### Next Quarter (Medium Priority -- Design System & Polish)

| # | Task | Impact |
|---|------|--------|
| 25 | Migrate all buttons from Bootstrap classes to design-system `.btn .btn-p/s/g/d` | Visual consistency |
| 26 | Migrate all tables to `.card-row` grid pattern from mockup | Visual consistency |
| 27 | Consolidate CSS tokens: rename `--radius`/`--radius-sm` to `--r`/`--r-xs`, add `--ease`/`--spring` | Token alignment with mockup |
| 28 | Add formal spacing scale tokens (`--sp-1` through `--sp-9`) | Consistent spacing |
| 29 | Add formal typography scale tokens (`--fs-xs` through `--fs-3xl`) | Consistent sizing |
| 30 | Add skeleton/shimmer loading states for all data-driven panels | Perceived performance |
| 31 | Add `aria-busy="true"` to loading containers | Accessible loading states |
| 32 | Add focus-visible styles to all custom interactive elements (`.dba-nav-item`, `.wf-step`, `.plan-chip`, `.fb-dd`, `.pg-btn`) | Keyboard navigation visibility |
| 33 | Replace `<a href="#">` with `<button>` for JS-only actions | Semantic correctness |
| 34 | Add `<fieldset>`/`<legend>` to form groups | Form accessibility |
| 35 | Responsive audit at 1024px, 768px, 375px for all pages | Mobile readiness |
| 36 | Convert remaining physical CSS to logical properties for full RTL | RTL robustness |
| 37 | Add `aria-sort` to sortable table columns | Sort state communication |
| 38 | Dashboard monolith: consider splitting into lazy-loaded panel modules | Performance, maintainability |

---

*End of audit report.*
