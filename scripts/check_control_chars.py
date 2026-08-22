"""Refuse source files containing C0 control characters.

A word-boundary regex shipped TWICE with literal backspace bytes where \b was
intended - once through a shell heredoc, once through a non-raw Python string -
and both were invisible in review diffs while silently killing the pattern's
English branch.  Control characters other than tab/newline/CR have no place in
this repository's text files, so their presence is an error, not a style note.
"""

from __future__ import annotations

import sys

ALLOWED = {0x09, 0x0A, 0x0D}


def main(paths: list[str]) -> int:
    bad = 0
    for path in paths:
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if b"\x00" in data:
            continue  # binary; not ours to police
        for index, byte in enumerate(data):
            if byte < 0x20 and byte not in ALLOWED:
                line = data.count(b"\n", 0, index) + 1
                print(f"{path}:{line}: control character 0x{byte:02X}")
                bad += 1
                break
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
