"""Verbatim-quote validation.

The promise docket makes is narrow and testable: every finding it publishes
carries a span of text that appears IN the source permit description. Not a
paraphrase, not a summary, not a tidied-up version. This module is the thing
that enforces it, and it is deliberately deterministic — no model is consulted
to decide whether a model told the truth.

Normalization rule (from scenario://docket/judgment-quote):

    digit-group spaces are normalized, NEVER inside quotes.

Seattle's permit prose splits numbers inconsistently ("92 000 sq ft" in one
record, "92,000" in the next), so a purely byte-exact match would reject honest
quotes over typesetting. We therefore collapse spaces that sit between digits —
but only in the regions of text that are NOT inside quotation marks. Text the
source itself quoted stays byte-exact on both sides of the comparison, because
rewriting the inside of a quote to make a match succeed is precisely the
dishonesty this validator exists to prevent.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A space (or run of spaces) with a digit on both sides: "92 000" -> "92000".
_DIGIT_GROUP_SPACE = re.compile(r"(?<=\d)[   ]+(?=\d)")

# Straight and curly quotation marks open/close a no-normalization region.
_QUOTE_CHARS = "\"'‘’“”"
_OPENERS = {'"': '"', "'": "'", "‘": "’", "“": "”"}


def _collapse_whitespace(text: str) -> str:
    """Runs of whitespace become one space; leading/trailing trimmed.

    This is typesetting, not content: the Socrata corpus is full of double
    spaces and stray newlines mid-sentence.
    """
    return re.sub(r"\s+", " ", text).strip()


def _segment_by_quotes(text: str) -> list[tuple[str, bool]]:
    """Split text into (segment, inside_quotes) runs.

    An unterminated opener means everything after it is treated as quoted —
    the conservative reading, since it withholds normalization rather than
    applying it where it may not belong.
    """
    segments: list[tuple[str, bool]] = []
    buf: list[str] = []
    closer: str | None = None

    for ch in text:
        if closer is None:
            if ch in _OPENERS:
                if buf:
                    segments.append(("".join(buf), False))
                    buf = []
                buf.append(ch)
                closer = _OPENERS[ch]
            else:
                buf.append(ch)
        else:
            buf.append(ch)
            if ch == closer:
                segments.append(("".join(buf), True))
                buf = []
                closer = None

    if buf:
        segments.append(("".join(buf), closer is not None))
    return segments


def normalize_outside_quotes(text: str) -> str:
    """Apply digit-group-space normalization only outside quoted regions.

    Whitespace collapsing and Unicode NFKC folding apply throughout — those
    change presentation, never which characters were claimed to be in the
    source. Digit regrouping could change a number, so it stops at a quote.
    """
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for segment, inside in _segment_by_quotes(text):
        out.append(segment if inside else _DIGIT_GROUP_SPACE.sub("", segment))
    return _collapse_whitespace("".join(out))


def strip_wrapping_quotes(quote: str) -> str:
    """Drop one layer of quotation marks the model wrapped around its span.

    Models return `"…"` as often as `…`. The wrapper is punctuation about the
    quote, not part of it; quotes nested INSIDE the span are left alone.
    """
    q = quote.strip()
    if len(q) >= 2 and q[0] in _QUOTE_CHARS:
        want = _OPENERS.get(q[0], q[0])
        if q[-1] == want:
            return q[1:-1].strip()
    return q


@dataclass(frozen=True)
class QuoteCheck:
    """The verdict, with enough detail for an operator to see why."""

    valid: bool
    reason: str
    quote: str = ""
    normalized_quote: str = ""

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "quote": self.quote,
            "normalized_quote": self.normalized_quote,
        }


def verify_quote(quote: str, source: str) -> QuoteCheck:
    """Does `quote` appear verbatim in `source`?

    Returns a QuoteCheck rather than a bool so the rejection reason travels
    with the rejection — an uncited judgment gets refused by name, not by a
    silent False.
    """
    if quote is None or not quote.strip():
        return QuoteCheck(False, "empty-quote")
    if source is None or not source.strip():
        return QuoteCheck(False, "empty-source", quote=quote)

    bare = strip_wrapping_quotes(quote)
    if not bare:
        return QuoteCheck(False, "empty-quote", quote=quote)

    needle = normalize_outside_quotes(bare)
    haystack = normalize_outside_quotes(source)

    if not needle:
        return QuoteCheck(False, "empty-quote", quote=quote)
    if needle in haystack:
        return QuoteCheck(True, "verbatim", quote=bare, normalized_quote=needle)

    # Case-insensitive is still a miss — we report it distinctly so the failure
    # mode ("the model retyped the casing") is visible instead of guessed at.
    if needle.casefold() in haystack.casefold():
        return QuoteCheck(False, "case-mismatch", quote=bare, normalized_quote=needle)
    return QuoteCheck(False, "not-in-source", quote=bare, normalized_quote=needle)
