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

# Both patterns are matched on privacy_form (see there). An address may use letters of any script
# (\w), because a Cyrillic or Greek letter that looks Latin still reads as an address.
EMAIL_RE = re.compile(r"[\w.%+-]+@[\w.-]+\.[^\W\d_]{2,}")
# US phone numbers with separators: (512) 555-0142, (571)555-0100, 512-555-0142, +1 512 555 0142,
# (571) 555 - 0100. Digit runs without separators (TABS2026012091, ZIP 78245, house number 17420)
# do not match, and neither does a 3-3-4 run inside a longer dashed number such as the Cook County
# parcel PIN 08-35-302-012-0000 or 08 - 35 - 302 - 012 - 0000 (no digit, or digit and dash, just
# before; no digit, or dash and digit, just after). A dash between a word and the number is
# punctuation, so the number in "call the office\u2014571-555-0100\u2014for tours" is found.
_PHONE_SEP = r"(?:\s?-\s?|[\s.])"
PHONE_RE = re.compile(
    r"(?<!\d)(?<!\d-)(?<!\d\s-)(?<!\d-\s)(?<!\d\s-\s)"
    rf"(?:\+?1{_PHONE_SEP}?)?(?:\(\d{{3}}\){_PHONE_SEP}?|\d{{3}}{_PHONE_SEP})"
    rf"\d{{3}}{_PHONE_SEP}\d{{4}}(?!-?\d)"
)
# What privacy_form writes as "-": every dash punctuation character (Unicode category Pd, among
# them the hyphen U+2010, the non-breaking hyphen U+2011 that pages put in phone numbers so they
# do not wrap, the figure, en and em dashes and the horizontal bar U+2012-U+2015) and these minus
# signs and hyphens of other categories (NFKD already folds U+FE63 and U+FF0D to "-", and the
# superscript and subscript minus to U+2212).
_HYPHENS = frozenset("\u02d7\u2043\u2212\u2796")
# What it writes as ".": middle dots, bullets, raised dots and the ideographic full stop (NFKD
# has already made U+0387, U+FF61 and U+FF65 into U+00B7, U+3002 and U+30FB).
_DOTS = frozenset("\u00b7\u2022\u2027\u2219\u22c5\u2e31\u2e33\u30fb\u3002")

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


def privacy_form(s: str) -> str:
    """The skeleton rule 7 matches: s without invisible characters, in NFKD with every combining
    mark dropped, dashes and minus signs written "-" and middle dots, bullets and ideographic full
    stops written ".". So jane.doe@exam\\u0301ple.com and 571\\u2011555\\u20110100 read as the
    address and the number a reader sees. Only for matching; stored text is not changed."""
    s = unicodedata.normalize("NFKD", strip_invisible(s))
    if s.isascii():
        return s
    out = []
    for ch in s:
        category = unicodedata.category(ch)
        if category[0] == "M":
            continue
        if category == "Pd" or ch in _HYPHENS:
            out.append("-")
        elif ch in _DOTS:
            out.append(".")
        else:
            out.append(ch)
    return "".join(out)


def find_personal_data(s: str, *, phones: bool = True) -> list[str]:
    """Email addresses and (unless phones=False) phone numbers found in s, in order of appearance.

    s is matched in its privacy_form, and the matches are returned in that form.
    """
    s = privacy_form(s)
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
