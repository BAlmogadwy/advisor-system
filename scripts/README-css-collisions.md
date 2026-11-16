# CSS collisions

`check_css_collisions.py` blocks one specific defect: **two rules with the same
selector, in the same at-rule context, both declaring the same property.** The
earlier declaration can never apply. That is the cascade, not a heuristic about
intent.

```bash
# scan everything (what CI runs)
python scripts/check_css_collisions.py --all-tracked

# after fixing some, tighten the record
python scripts/check_css_collisions.py --all-tracked --update-baseline
```

The one case where a same-selector repeat is deliberate is a **fallback across
rules**:

```css
.a { height: 100vh; }   /* flagged */
.a { height: 100dvh; }  /* a browser that rejects dvh keeps the first */
```

That is rare, and the baseline absorbs the existing ones. If you write a new
one, merge it into a single rule — two declarations of a property inside *one*
rule are understood as a fallback stack and are never flagged.

The baseline (`css_collision_baseline.json`, 162 entries) holds the collisions
that predate the gate. It is keyed by file, context, selector and property —
never by line number — so editing one part of a stylesheet does not invalidate
the record of another.

**The list may only shrink.** `--update-baseline` prunes entries that no longer
collide and *refuses* to add any; it also preserves entries for files it did not
scan, so a single-file run cannot wipe the rest. Stale entries are reported but
never fail a run, so fixing something never breaks a commit.

`--rebaseline` exists only for creating the list, or after changing how the
checker parses CSS. It adopts every current collision wholesale, so it is never
the right answer to a failing run.

The MRI stylesheets are excluded by name: the owner has ruled those pages
off-limits, and this gate must never be the reason one of them has to change.

## What this does not catch, and why

The sibling defect is worse and is **not** covered: two *different* selectors
that overlap, where the later or more specific one silently wins.

```css
.table-wrap thead th            { position: sticky; }   /* (0,1,2) */
.table-wrap thead th[data-sort] { position: relative; } /* (0,2,2) — wins */
```

That shipped, and it meant the sticky header had never worked on a sortable
column — i.e. nearly every column, app-wide.

It cannot be separated mechanically from the override every stylesheet performs
on purpose. A detector for it — equal specificity, overlapping subjects,
crossing qualifiers, same property — reported **15,399 pairs** on `global.css`,
essentially all of them intentional (`.ux-toast.success` then
`.ux-toast.error`, `.sp-c-demand` then `.sp-c-sections`, and so on). A gate
nobody can read is a gate that gets switched off.

Two things cover that case instead:

1. **Verify in the browser, not in the source.** Read the computed value. Four
   of the five November collisions were found that way, and one was found only
   because the browser reported `position: relative` where the source clearly
   said `sticky`.
2. **A sticky `th` needs a scrolling ancestor.** With none, it anchors to the
   viewport and floats over the page. `position: sticky` in a shared rule is
   therefore opt-in per container, never global.

### One latent trap worth knowing about

Running the overlap query once, by hand, surfaced this pair:

```css
.table-wrap thead th            { position: sticky; }   /* (0,1,2) */
.tbl-card thead th[data-sort]   { position: relative; } /* (0,2,2) — wins */
```

Today it is unreachable: no `.tbl-card` carrying `data-sort` sits inside a
`.table-wrap`. The sortable tables are either `.tbl-card` outside a wrap
(Advisor Portfolio, Batch, Prereq, Section Planning) or Bootstrap `.table`
inside one (Plan, User Management). Instructor Management is the only page
nesting `.tbl-card` in a `.table-wrap`, and its headers are not sortable.

No rule was added for it, because unreachable CSS is the same disease. But
adding `data-sort` to the Instructor Management tables would silently unstick
exactly those headers and leave the rest pinned. If you do that, give them
`position: sticky` back at a specificity above `(0,2,2)`.

If you want the overlap query for a one-off audit, it is a ~60-line script:
group declarations by property, keep pairs whose subject compounds can match
one element, filter to equal specificity, and read the output by hand. It is
useful once, after a large refactor. It is not useful on every commit.
