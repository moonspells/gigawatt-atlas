"""Text hygiene: invisible characters, personal-data patterns and name normalization (07 §5.3, §6.6)."""

from __future__ import annotations

import re
import unicodedata

# Zero-width and bidirectional control characters that can hide text or reorder it on screen.
INVISIBLE_RE = re.compile("[​-‍⁠﻿‎‏؜‪-‮⁦-⁩]")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# US phone numbers with separators. Digit runs without separators (TABS2026012091, ZIP 78245,
# house number 17420) do not match.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")

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


def strip_invisible(s: str) -> str:
    """Remove zero-width and bidirectional control characters."""
    return INVISIBLE_RE.sub("", s)


def find_personal_data(s: str) -> list[str]:
    """Email addresses and phone numbers found in s, in order of appearance."""
    found = [(m.start(), m.group(0)) for m in EMAIL_RE.finditer(s)]
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
