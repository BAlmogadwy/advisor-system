"""Canonical student identifiers and university email addresses.

Student email usernames changed for the 45 admission cohort.  Keep the rule in
one place so login and messaging channels cannot silently disagree about where
an authentication code is delivered.
"""

from __future__ import annotations

import re
import unicodedata

from django.conf import settings

_MAX_DJANGO_INTEGER = 2_147_483_647
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def normalize_student_id(value: object) -> int:
    """Return one positive, database-safe student ID using ASCII digits.

    Decimal digits from Arabic and other Unicode scripts are accepted because
    students may enter them through an Arabic keyboard.  Signs, separators,
    booleans, floats, embedded whitespace, and other numeric-looking Unicode
    characters are rejected rather than guessed.
    """

    if isinstance(value, bool):
        raise ValueError("student ID must be decimal digits")

    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate or len(candidate) > 10:
            raise ValueError("student ID must be decimal digits")
        digits: list[str] = []
        try:
            for character in candidate:
                digits.append(str(unicodedata.decimal(character)))
        except (TypeError, ValueError) as exc:
            raise ValueError("student ID must be decimal digits") from exc
        normalized = int("".join(digits), 10)
    else:
        raise ValueError("student ID must be decimal digits")

    if not 0 < normalized <= _MAX_DJANGO_INTEGER:
        raise ValueError("student ID is outside the supported range")
    return normalized


def _email_domain(domain: str | None) -> str:
    raw = settings.STUDENT_EMAIL_DOMAIN if domain is None else domain
    normalized = str(raw or "").strip().lower()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    if not _DOMAIN_RE.fullmatch(normalized):
        raise ValueError("student email domain is invalid")
    return normalized


def student_email(student_id: object, *, domain: str | None = None) -> str:
    """Return the canonical university mailbox for a student cohort.

    Admission prefixes through 44 use ``ID@domain``.  Prefixes 45 and newer use
    ``tuID@domain``.  The normalized ASCII ID is always used in the address.
    """

    normalized_id = normalize_student_id(student_id)
    ascii_id = str(normalized_id)
    prefix = int(ascii_id[:2])
    local_part = ascii_id if prefix <= 44 else f"tu{ascii_id}"
    return f"{local_part}@{_email_domain(domain)}"
