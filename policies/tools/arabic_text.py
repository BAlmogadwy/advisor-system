"""Re-export of the canonical Arabic normaliser, which now lives in ``core``.

The implementation moved to ``core/services/arabic_text.py`` when the policy store
was wired into the adviser runtime: the runtime needs it to resolve Arabic queries
onto policy topics, and a second copy here is exactly the duplication that once let
the two versions diverge and silently normalise every string to "".

These tools run standalone (``python policies/tools/x.py``) rather than under
Django, so the import is a path fix-up, not a Django dependency — the canonical
module imports nothing but ``re``.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.services.arabic_text import (  # noqa: E402
    STOPWORDS,
    all_tokens,
    content_tokens,
    normalise,
)

__all__ = ["STOPWORDS", "all_tokens", "content_tokens", "normalise"]
