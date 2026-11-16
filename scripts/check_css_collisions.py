"""Refuse new CSS rules whose declarations are dead on arrival.

`static/css/global.css` is ~16k lines, and it keeps producing one defect:
a rule is written, and a later rule with the SAME selector re-declares the
same property.  Nothing errors.  The earlier declaration is simply gone, and
the page renders as though it had never been written.  Five of these were
found in a single piece of work in November 2026 -- including
`.table-wrap thead th`'s sticky header, which had been dead app-wide for
however long, on every sortable column.

What this checks
----------------
Two rules in the same at-rule context, with the same selector, both declaring
the same property.  The earlier declaration cannot ever apply, so it is
either dead code or a bug.  That is the cascade, not a heuristic about what
the author probably meant.

The one deliberate case it still flags is a fallback written across two rules
(`height:100vh` then `height:100dvh`); write those as one rule, where two
declarations of a property are understood as a fallback stack.

What this deliberately does NOT check
-------------------------------------
The nastier sibling, where two DIFFERENT selectors overlap and the later or
more specific one silently wins -- `.table-wrap thead th` beaten by
`.table-wrap thead th[data-sort]`.  That cannot be separated mechanically
from the override every stylesheet does on purpose (`.btn` then `.btn:hover`,
`.ux-toast.success` then `.ux-toast.error`).  Measured on this stylesheet, a
detector for it reported 15,399 pairs, essentially all of them intentional.
A gate nobody can read is a gate that gets switched off, so it is not here.
`scripts/README-css-collisions.md` records the overlap query for one-off use.

Existing collisions are recorded in the baseline beside this script, so the
gate only ever blocks NEW ones.  It never fails because something was fixed:
entries that no longer collide are reported as stale, and `--update-baseline`
prunes them while REFUSING to adopt any new ones, so the list may only shrink.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "css_collision_baseline.json"

#: The owner has ruled the MRI stylesheets off-limits, so the gate must never
#: be the reason one of them has to change.
EXCLUDED_NAMES = ("timetable-mri.css", "timetable-mri-full.css")

BS = chr(92)


@dataclass
class Rule:
    context: tuple
    selector: str
    line: int
    decls: dict
    order: int


def strip_comments(src: str) -> str:
    """Blank out comments, preserving line count and string literals."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        if src[i] == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("\n" * src.count("\n", i, end))
            i = end
        elif src[i] in "\"'":
            quote = src[i]
            j = i + 1
            while j < n and src[j] != quote:
                if src[j] == BS:
                    j += 1
                j += 1
            out.append(src[i : j + 1])
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


PROP_RE = re.compile(r"-{0,2}[a-z][a-z0-9-]*$")
CUSTOM_PROP_RE = re.compile(r"--[\w-]+$")
IMPORTANT_RE = re.compile(r"!\s*important\b", re.IGNORECASE)


def _without_literals(text: str) -> str:
    """`text` with every quoted string blanked out."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "\"'":
            quote = text[i]
            i += 1
            while i < n and text[i] != quote:
                if text[i] == BS:
                    i += 1
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _record(decls: dict, chunk: str) -> None:
    chunk = chunk.strip()
    if not chunk or ":" not in chunk:
        return
    prop, _, value = chunk.partition(":")
    prop = prop.strip()
    if prop.startswith("--"):
        # Custom property names are case-SENSITIVE: `--Foo` and `--foo` are
        # two different properties, so lowercasing them would invent a
        # collision between them.
        if not CUSTOM_PROP_RE.match(prop):
            return
    else:
        prop = prop.lower()
        if not PROP_RE.match(prop):
            return
    # `! important` with a space is valid CSS, and `content:"!important"` is
    # not important at all -- so match the flag outside string literals only.
    important = bool(IMPORTANT_RE.search(_without_literals(value)))
    # Two declarations of one property inside ONE rule are the author writing
    # a fallback stack on purpose; keep the strongest importance seen.
    decls[prop] = decls.get(prop, False) or important


def split_decls(body: str) -> dict:
    """property -> is_important for the declarations directly in `body`.

    Declarations inside a nested block belong to that block, not this one.
    """
    decls: dict = {}
    parens = brackets = braces = 0
    buf: list = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c in "\"'":
            j = i + 1
            while j < n and body[j] != c:
                if body[j] == BS:
                    j += 1
                j += 1
            buf.append(body[i : j + 1])
            i = j + 1
            continue
        if c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1
        elif c == "{":
            braces += 1
            buf = []
            i += 1
            continue
        elif c == "}":
            braces -= 1
            buf = []
            i += 1
            continue
        if c == ";" and not parens and not braces and not brackets:
            _record(decls, "".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    _record(decls, "".join(buf))
    return decls


def parse_rules(src: str) -> list:
    clean = strip_comments(src)
    rules: list = []
    stack: list = []
    newlines = [0]
    for index, char in enumerate(clean):
        if char == "\n":
            newlines.append(index)

    def line_of(pos: int) -> int:
        lo, hi = 0, len(newlines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if newlines[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    i = 0
    n = len(clean)
    prelude_start = 0
    order = 0
    while i < n:
        c = clean[i]
        if c in "\"'":
            j = i + 1
            while j < n and clean[j] != c:
                if clean[j] == BS:
                    j += 1
                j += 1
            i = j + 1
            continue
        if c == "{":
            prelude = clean[prelude_start:i].strip()
            stack.append(("AT" if prelude.startswith("@") else "RULE", prelude, i))
            prelude_start = i + 1
        elif c == "}":
            if stack:
                kind, prelude, start = stack.pop()
                if kind == "RULE":
                    rules.append(
                        Rule(
                            context=tuple(x[1] for x in stack if x[0] == "AT"),
                            selector=prelude,
                            line=line_of(start),
                            decls=split_decls(clean[start + 1 : i]),
                            order=order,
                        )
                    )
                    order += 1
            prelude_start = i + 1
        elif c == ";":
            prelude_start = i + 1
        i += 1
    return rules


def _scan_literal(text: str, start: int) -> int:
    """Index just past the string literal opening at `start`."""
    quote = text[start]
    i = start + 1
    n = len(text)
    while i < n and text[i] != quote:
        if text[i] == BS:
            i += 1
        i += 1
    return i + 1


def split_selector_list(selector: str) -> list:
    """Split a selector list on TOP-LEVEL commas only.

    `.a:is(.x, .y)` is one selector, not two, and so is `[data-k="a,b"]`.
    A naive split produced baseline keys like `td)` and `.btn-success)`,
    which both mask real collisions and collide with each other.
    """
    parts: list = []
    buf: list = []
    parens = brackets = 0
    i = 0
    n = len(selector)
    while i < n:
        c = selector[i]
        if c in "\"'":
            end = _scan_literal(selector, i)
            buf.append(selector[i:end])
            i = end
            continue
        if c == "(":
            parens += 1
        elif c == ")":
            parens = max(0, parens - 1)
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets = max(0, brackets - 1)
        if c == "," and not parens and not brackets:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _norm(selector: str) -> str:
    """Collapse whitespace and tighten combinators, leaving strings alone.

    `[a="x > y"]` and `[a="x>y"]` are different selectors, so the rewrite
    must not reach inside a quoted attribute value.
    """
    pieces: list = []
    buf: list = []
    i = 0
    n = len(selector)
    while i < n:
        c = selector[i]
        if c in "\"'":
            end = _scan_literal(selector, i)
            pieces.append(("code", "".join(buf)))
            pieces.append(("literal", selector[i:end]))
            buf = []
            i = end
            continue
        buf.append(c)
        i += 1
    pieces.append(("code", "".join(buf)))

    out = []
    for kind, text in pieces:
        if kind == "literal":
            out.append(text)
        else:
            text = re.sub(r"\s+", " ", text)
            out.append(re.sub(r"\s*([>+~])\s*", r"\1", text))
    return "".join(out).strip()


def collisions_in(src: str) -> list:
    """(context, selector, property, dead_line, winning_line), source order."""
    index = defaultdict(dict)
    for rule in parse_rules(src):
        for part in split_selector_list(rule.selector):
            selector = _norm(part)
            if selector:
                # Keyed by rule identity, so `.a, .a { }` -- one rule naming
                # a selector twice -- cannot collide with itself, and two
                # rules sharing one physical line still can.
                index[(rule.context, selector)][rule.order] = rule

    found = []
    for (context, selector), by_order in index.items():
        rules = [by_order[k] for k in sorted(by_order)]
        if len(rules) < 2:
            continue
        # For each property, the LAST rule declaring it is the live one; every
        # earlier declaration of that property is dead.
        for prop in {p for r in rules for p in r.decls}:
            declaring = [r for r in rules if prop in r.decls]
            if len(declaring) < 2:
                continue
            winner = declaring[-1]
            for dead in declaring[:-1]:
                if dead.decls[prop] and not winner.decls[prop]:
                    continue  # an earlier !important still applies
                found.append((context, selector, prop, dead.line, winner.line))
    found.sort(key=lambda f: (f[3], f[1], f[2]))
    return found


def key_for(rel_path: str, context: tuple, selector: str, prop: str) -> str:
    """Baseline key, deliberately free of line numbers so that editing one
    part of a stylesheet does not invalidate the record of another."""
    return "|".join((rel_path, " ".join(context), selector, prop))


def _write_baseline(entries: list) -> None:
    BASELINE_PATH.write_text(
        json.dumps(
            {
                "comment": (
                    "Collisions that predate scripts/check_css_collisions.py. "
                    "The gate blocks NEW ones only. --update-baseline prunes "
                    "entries that no longer collide and REFUSES to add any, so "
                    "this list may only shrink."
                ),
                "entries": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _rel(path) -> str:
    """Repo-relative posix form, or "" if the path is not inside the repo."""
    try:
        full = REPO / path if not Path(path).is_absolute() else Path(path)
        return full.resolve().relative_to(REPO).as_posix()
    except (ValueError, OSError):
        return ""


def css_files(paths: list, all_tracked: bool) -> list:
    if all_tracked or not paths:
        tracked = subprocess.run(
            ["git", "ls-files", "*.css"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO,
        ).stdout.splitlines()
        paths = [p for p in tracked if p.startswith("static/css/")]
    return [p for p in paths if Path(p).name not in EXCLUDED_NAMES]


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument(
        "--all-tracked",
        action="store_true",
        help="scan every tracked stylesheet instead of an argument list",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="prune entries that no longer collide; refuses to add new ones",
    )
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help=(
            "record EVERY current collision as known. Only for creating the "
            "baseline, or after changing how the checker parses; it adopts "
            "real defects wholesale, so it is never the answer to a failing run"
        ),
    )
    args = parser.parse_args(argv)

    paths = css_files(args.paths, args.all_tracked)
    if not paths:
        return 0

    baseline = {}
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    known = set(baseline.get("entries", []))

    live = {}
    unreadable = set()
    for path in paths:
        full = REPO / path if not Path(path).is_absolute() else Path(path)
        try:
            rel = full.resolve().relative_to(REPO).as_posix()
        except ValueError:
            print(f"{path}: outside the repository; skipped", file=sys.stderr)
            continue
        try:
            src = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Its baseline entries must survive: dropping them would quietly
            # un-baseline collisions nobody has fixed.
            unreadable.add(rel)
            continue
        for context, selector, prop, dead_line, win_line in collisions_in(src):
            live[key_for(rel, context, selector, prop)] = (
                rel,
                context,
                selector,
                prop,
                dead_line,
                win_line,
            )

    if args.rebaseline:
        if not args.all_tracked:
            print("--rebaseline requires --all-tracked", file=sys.stderr)
            return 2
        _write_baseline(sorted(live))
        print(f"baseline rebuilt from scratch: {len(live)} entries")
        return 0

    if args.update_baseline:
        scanned_files = {k.split("|", 1)[0] for k in live} | {
            p for p in (_rel(x) for x in paths) if p
        }
        # The list may only SHRINK. Two ways it could grow, both guarded here:
        # a new collision in a scanned file would be adopted silently, and a
        # partial scan would drop every other file's entries.
        kept = {k for k in live if k in known}
        untouched = {
            k
            for k in known
            if k.split("|", 1)[0] not in scanned_files or k.split("|", 1)[0] in unreadable
        }
        adopted = sorted(k for k in live if k not in known)
        entries = sorted(kept | untouched)
        if adopted:
            print(
                f"refusing to write: {len(adopted)} collision(s) are NOT in the"
                " baseline. Fix them, or state plainly why they belong there"
                " and add them by hand.",
                file=sys.stderr,
            )
            for key in adopted[:20]:
                print(f"  {key}", file=sys.stderr)
            return 1
        _write_baseline(entries)
        print(f"baseline written: {len(entries)} entries ({len(known) - len(entries)} pruned)")
        return 0

    scanned = {p for p in (_rel(x) for x in paths) if p}
    new = [v for k, v in sorted(live.items()) if k not in known]
    for rel, context, selector, prop, dead_line, win_line in new:
        where = f" in `{' / '.join(context)}`" if context else ""
        print(
            f"{rel}:{dead_line}: `{prop}` on `{selector}`{where} is dead -- "
            f"the same selector re-declares it at line {win_line}"
        )

    # Stale entries are an improvement, never a failure.
    stale = sorted(
        k
        for k in known
        if k not in live and k.split("|", 1)[0] in scanned and k.split("|", 1)[0] not in unreadable
    )
    if stale:
        print(
            f"note: {len(stale)} baseline entr{'y is' if len(stale) == 1 else 'ies are'}"
            " now fixed; run"
            " `python scripts/check_css_collisions.py --update-baseline` to tighten"
            " the gate.",
            file=sys.stderr,
        )

    if new:
        print(
            f"\n{len(new)} new CSS collision(s). The earlier declaration never"
            " applies: either delete it, or merge the two rules.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
