"""Shared Arabic normalisation for the policy store's comparison tools.

Lives in one module because it was originally duplicated across two scripts, the
copies diverged, and one of them acquired a diacritics character range that spanned
the entire Arabic alphabet — silently normalising every string to the empty string
and reporting 64 of 81 rules as having no evidence. The failure was invisible
because "no tokens" and "no match" look identical downstream.

Normalisation is deliberately NARROW. It folds only the things a legitimate
transcription may differ on:
  * Arabic-Indic digits and separators -> ASCII  (٤٫٧٥ -> 4.75, ٢٥٪ -> 25%)
  * diacritics and tatweel             -> removed
  * alef/ya/ta-marbuta/hamza variants  -> unified
  * Arabic punctuation                 -> whitespace

It does NOT fold word boundaries or repair spelling, because those are exactly the
transcription errors these tools exist to catch.
"""

from __future__ import annotations

import re

# Arabic combining marks ONLY. Deliberately enumerated rather than expressed as a
# broad range: the Arabic letters live at U+0620-U+064A, immediately adjacent to
# the marks at U+064B-U+0652, and an off-by-one range boundary here deletes the
# language. Do not "simplify" this into a single range.
_DIACRITICS = re.compile(
    "["
    "ً-ْ"  # fathatan..sukun
    "ٓ-ٕ"  # maddah, hamza above/below
    "ٰ"  # superscript alef
    "ۖ-ۜ۟-۪ۨ-ۭ"  # Quranic marks
    "ـ"  # tatweel
    "]"
)

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٪", "0123456789.%")

# Arabic punctuation sits INSIDE the Arabic block, so a naive "\w or Arabic" word
# class counts ، ؛ ؟ as letters and every clause separator becomes a phantom token.
_AR_PUNCT = re.compile("[،؛؟٬۔٭﴾﴿]")

# '.' and '%' survive the word class so that ٤٫٧٥ -> "4.75" and ٢٥٪ -> "25%" stay
# single tokens; a decimal point split into two tokens turns 4.75 and 475 into the
# same thing. Sentence-final periods are stripped per-token instead.
_NON_WORD = re.compile(r"[^\w؀-ۿ.%]+")

# High-frequency function words. Removed before overlap scoring so that two
# unrelated sentences on the same page do not look similar merely by sharing في/من.
_STOPWORDS_SOURCE = "في من على أو و إلى عن أن لا ما هو التي الذي مع بين كل قد هذا ذلك عند به له"


def normalise(text: str) -> str:
    """Fold a string to its comparison form. Never returns None."""
    text = str(text or "").translate(_AR_DIGITS)
    text = _AR_PUNCT.sub(" ", text)
    text = _DIACRITICS.sub("", text)
    text = re.sub("[أإآٱ]", "ا", text)  # alef variants -> ا
    text = re.sub("[ىئ]", "ي", text)  # alef maqsura / ya-hamza -> ي
    text = text.replace("ة", "ه")  # ta marbuta -> ه
    text = text.replace("ؤ", "و")  # waw-hamza -> و
    return _NON_WORD.sub(" ", text).strip()


# Folded through normalise() at definition. The list is written the way the words
# are spelled — على، إلى، أن، أو — but content_tokens() compares AFTER folding, where
# they are علي، الي، ان، او. Stored raw, those four never matched anything and four of
# the commonest words in Arabic were treated as content in every query and every
# record: they entered the token index, diluted the IDF denominator, and could carry
# a match on their own.
STOPWORDS = frozenset(normalise(word) for word in _STOPWORDS_SOURCE.split())


def _split(text: str) -> list[str]:
    return [t for t in (tok.strip(".") for tok in normalise(text).split()) if t]


def content_tokens(text: str) -> set[str]:
    """Meaningful tokens for overlap scoring: no stopwords, no single characters."""
    return {t for t in _split(text) if len(t) > 1 and t not in STOPWORDS}


def all_tokens(text: str) -> list[str]:
    """Every token in order, for recall scoring where omissions matter."""
    return _split(text)


def _self_test() -> None:
    """Guards the failure that motivated this module: normalising to nothing."""
    probe = "تنقطع المكافأة عن الطالب المعتذر"
    got = normalise(probe)
    assert got, "normalise() destroyed the input — check the diacritics range"
    assert "الطالب" in got, f"letters lost: {got!r}"
    assert normalise("٤٫٧٥") == "4.75", normalise("٤٫٧٥")
    assert normalise("٢٥٪") == "25%", normalise("٢٥٪")
    assert content_tokens(probe) == {"تنقطع", "المكافاه", "الطالب", "المعتذر"}, content_tokens(
        probe
    )


_self_test()
