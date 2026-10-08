"""Text hygiene: invisible characters, personal-data patterns and name normalization (07 §5.3, §6.6)."""

from __future__ import annotations

import re
import unicodedata

# Characters that render as nothing but survive in text, so they can split an email address past
# a regex, reorder text on screen or smuggle instructions to a downstream model (07 §8, 10 §11.8):
# every format character (Unicode category Cf: zero-width and bidirectional controls, soft hyphen,
# word joiner and invisible operators, interlinear annotations, the tag characters U+E0001 and
# U+E0020-E007F), lone surrogates (Cs, which cannot be written as UTF-8), and these invisible
# characters of other categories: combining grapheme joiner, Hangul fillers, Khmer inherent
# vowels, Mongolian free variation selectors, variation selectors and the rest of plane 14.
_HIDDEN_CATEGORIES = frozenset({"Cf", "Cs"})
_HIDDEN_EXTRA_RE = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0\U000e0000-\U000e0fff]"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# US phone numbers with separators: (512) 555-0142, (571)555-0100, 512-555-0142, +1 512 555 0142.
# Digit runs without separators (TABS2026012091, ZIP 78245, house number 17420) do not match, and
# neither does a 3-3-4 run inside a longer dashed number such as the Cook County parcel PIN
# 08-35-302-012-0000.
PHONE_RE = re.compile(
    r"(?<![\d-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![-\d])"
)

_NON_WORD_RE = re.compile(r"[\W_]+")
_ORG_SUFFIXES = (
    "inc",
    "llc",
    "l l c",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "lp",
    "holdings",
)
_ORG_SUFFIX_RE = re.compile(r"\s(?:" + "|".join(re.escape(s) for s in _ORG_SUFFIXES) + r")$")


def is_hidden(ch: str) -> bool:
    """True for one character that strip_invisible removes."""
    return unicodedata.category(ch) in _HIDDEN_CATEGORIES or bool(_HIDDEN_EXTRA_RE.match(ch))


def hidden_characters(s: str) -> list[str]:
    """The characters of s that strip_invisible removes, in order (empty for ASCII)."""
    if s.isascii():
        return []
    return [ch for ch in s if is_hidden(ch)]


def strip_invisible(s: str) -> str:
    """Remove invisible, format and bidirectional control characters and lone surrogates."""
    if s.isascii():
        return s
    return "".join(ch for ch in s if not is_hidden(ch))


def published_form(s: str) -> str:
    """The text the privacy rule checks: NFKC of s without invisible characters, so a split
    (jane.doe\\u200b@example.com) or fullwidth (\\uff20) address is still found."""
    return unicodedata.normalize("NFKC", strip_invisible(s))


def find_personal_data(s: str, *, phones: bool = True) -> list[str]:
    """Email addresses and (unless phones=False) phone numbers found in s, in order of appearance.

    s is matched as given; callers that check stored text pass published_form(s).
    """
    found = [(m.start(), m.group(0)) for m in EMAIL_RE.finditer(s)]
    if phones:
        found += [(m.start(), m.group(0)) for m in PHONE_RE.finditer(s)]
    return [text for _, text in sorted(found)]


def normalize_name(s: str) -> str:
    """NFKC, casefold, punctuation to spaces, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", strip_invisible(s)).casefold()
    return " ".join(_NON_WORD_RE.sub(" ", s).split())


def normalize_org(s: str) -> str:
    """normalize_name, then trailing legal-form words dropped ("Foo Holdings LLC" -> "foo").

    A name that is only a legal-form word is kept as it is.
    """
    name = normalize_name(s)
    while True:
        shorter = _ORG_SUFFIX_RE.sub("", name)
        if shorter == name or not shorter:
            return name
        name = shorter
