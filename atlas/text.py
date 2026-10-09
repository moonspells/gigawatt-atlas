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
# parcel PIN 08-35-302-012-0000 or 08 - 35 - 302 - 012 - 0000: never a digit, or a digit and a
# dash, just before, nor a digit, or a dash and a digit, just after; and a number whose own
# separators are spaced dashes (the spaced PIN's form) also not after a digit and a spaced dash.
# So a dash that sets the number off from a word or from another number is punctuation: the
# numbers in "Suite 5 - 571-555-0100", "Building 2 \u2014 703-555-0123" and "call the
# office\u2014571-555-0100\u2014for tours" are found (privacy_form writes the em dash " - ").


def _phone(sep: str) -> str:
    return rf"(?:\+?1{sep}?)?(?:\(\d{{3}}\){sep}?|\d{{3}}{sep})\d{{3}}{sep}\d{{4}}"


PHONE_RE = re.compile(
    r"(?<!\d)(?<!\d-)(?:"
    + _phone(r"[\s.-]")
    + r"|(?<!\d\s-)(?<!\d-\s)(?<!\d\s-\s)"
    + _phone(r"(?:\s?-\s?|[\s.])")
    + r")(?!-?\d)"
)
# What privacy_form writes as "-": every dash punctuation character (Unicode category Pd, among
# them the hyphen U+2010, the non-breaking hyphen U+2011 that pages put in phone numbers so they
# do not wrap and the figure dash U+2012), these minus signs and hyphens of other categories (NFKD
# already folds U+FE63 and U+FF0D to "-", and the superscript and subscript minus to U+2212), and
# horizontal lines that read as a dash between digits (box drawing U+2500 and U+2501, U+23AF, and
# the katakana prolonged sound mark U+30FC, which NFKD also makes of U+FF70).
_HYPHENS = frozenset("\u02d7\u2043\u2212\u2796\u2500\u2501\u23af\u30fc")
# Dashes that are punctuation, not hyphens: the en and em dashes, the horizontal bar and the two-
# and three-em dashes (NFKD makes U+FE58, U+FE31 and U+FE32 into em and en dashes). privacy_form
# writes them " - ", so "Building 2\u2014571-555-0100" and "571-555-0100\u20142nd floor" read
# like "Building 2 - 571-555-0100", not like one dashed number, and 571\u2013555\u20130100 reads
# as 571 - 555 - 0100, which PHONE_RE finds too.
_SPACED_DASHES = frozenset("\u2013\u2014\u2015\u2e3a\u2e3b")
# What it writes as ".": middle dots, bullets, raised dots and full stops of other scripts (the
# ideographic, Armenian, Arabic, Ethiopic, Mongolian and stenographic full stops, the runic
# single punctuation, the Canadian syllabics final middle dot and the sinological dot); NFKD has
# already made U+0387, U+FF61 and U+FF65 into U+00B7, U+3002 and U+30FB.
_DOTS = frozenset(
    "\u00b7\u2022\u2027\u2219\u22c5\u2e31\u2e33\u30fb\u3002"
    "\u0589\u06d4\u1362\u1803\u16eb\u1427\ua78f\u2e3c"
)
# Control characters (Cc) that are not whitespace: NUL to BS, SO to ESC, DEL, and C1 except NEL.
_NON_SPACE_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f]")

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


def clean_text(s: str) -> str:
    """Upstream text as an importer stores it: strip_invisible, every control character that is
    not whitespace removed (NUL, BEL, ESC, DEL and the C1 controls but NEL), and each run of
    whitespace (tab, newline, CR, NEL, U+2028 and the rest) made one space, trimmed. The result
    has none of the characters the text rule rejects, so one stray byte upstream cannot hold a
    record."""
    s = _NON_SPACE_CONTROL_RE.sub("", strip_invisible(s))
    return " ".join(s.split())


def privacy_form(s: str) -> str:
    """The skeleton rule 7 matches: s without invisible characters, in NFKD with every combining
    mark dropped, hyphens and minus signs written "-", en and em dashes written " - " (a space on
    a side that has none), and middle dots, bullets and full stops of other scripts written ".".
    So jane.doe@exam\\u0301ple.com and 571\\u2011555\\u20110100 read as the address and the
    number a reader sees. Only for matching; stored text is not changed."""
    s = unicodedata.normalize("NFKD", strip_invisible(s))
    if s.isascii():
        return s
    out: list[str] = []
    for i, ch in enumerate(s):
        category = unicodedata.category(ch)
        if category[0] == "M":
            continue
        if ch in _SPACED_DASHES:
            if out and not out[-1].isspace():
                out.append(" ")
            out.append("-")
            if i + 1 < len(s) and not s[i + 1].isspace():
                out.append(" ")
        elif category == "Pd" or ch in _HYPHENS:
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
