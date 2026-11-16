"""Refuse source files containing C0 control characters.

A word-boundary regex shipped TWICE with literal backspace bytes where \b was
intended - once through a shell heredoc, once through a non-raw Python string -
and both were invisible in review diffs while silently killing the pattern's
English branch.  Control characters other than tab/newline/CR have no place in
this repository's text files, so their presence is an error, not a style note.
"""

from __future__ import annotations

import subprocess
import sys

ALLOWED = {0x09, 0x0A, 0x0D}

#: Hash-pinned scraped evidence; mirrors the pre-commit excludes.
EXCLUDED_PREFIXES = ("policies/sources/",)


def tracked_files() -> list[str]:
    """Every git-tracked path, minus the excluded evidence directories.

    Exists because Windows caps the command line well below 900 paths - the
    CI test invokes `--all-tracked` instead of an argument list.
    """
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [p for p in out if not p.startswith(EXCLUDED_PREFIXES)]


def main(paths: list[str]) -> int:
    if paths == ["--all-tracked"]:
        paths = tracked_files()
    bad = 0
    for path in paths:
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        # A NUL used to mean "binary, not ours to police" -- and that let the
        # worst case straight through: `content:'\00b7'` in a Python-generated
        # stylesheet lands as NUL + "b7", so the one file that most needed
        # checking was the one file skipped. Decodability is the honest test:
        # real binaries are not valid UTF-8, while a text file carrying a stray
        # NUL decodes fine and is exactly what this hook is for.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for index, byte in enumerate(data):
            if byte < 0x20 and byte not in ALLOWED:
                line = data.count(b"\n", 0, index) + 1
                print(f"{path}:{line}: control character 0x{byte:02X}")
                bad += 1
                break
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
