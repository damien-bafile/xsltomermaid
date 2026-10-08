"""Fuzzy table search: by table name, and by the columns a table has.

Pure Python (no Qt). Each word of the query must match the table, either its
name (fuzzily: the letters in order, as in fzf) or one of its column names
(contained, for words of three letters or more). Matches are scored so the
best come first: exact and prefix names, then names containing the word,
then letters in order with few gaps, then column matches.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fewer letters than this match columns only exactly-contained and never
# fuzzily: "id" would otherwise match nearly every Dynamics column.
COLUMN_MIN = 3


@dataclass(frozen=True)
class Match:
    """How a table matched a query: higher ``score`` is better."""

    score: int
    column: str = ""  # a column that matched, when the name didn't


def _boundary(text: str, i: int) -> bool:
    """A word starts at ``text[i]``: the start, after _ . - space, or a
    lower-to-upper case change (camelCase)."""
    if i == 0:
        return True
    before, here = text[i - 1], text[i]
    return before in "_ .-" or (before.islower() and here.isupper()) or (
        before.isalpha() != here.isalpha()
    )


def fuzzy_score(term: str, text: str) -> int | None:
    """How well ``term`` matches ``text`` (case-insensitive), or None.

    Contained terms score highest (more at the start or a word start);
    otherwise the letters must appear in order, scored up for runs of
    consecutive letters and word starts, down for gaps. A match spread too
    thin (``term`` letters scattered over a long name) doesn't count.
    """
    if not term:
        return 0
    low_term, low = term.lower(), text.lower()
    if low_term == low:
        return 1000
    at = low.find(low_term)
    if at >= 0:
        score = 600 + 10 * len(low_term) - min(at, 50)
        if at == 0:
            score += 150
        elif _boundary(text, at):
            score += 80
        return score
    if len(low_term) < 2:
        return None
    # Letters in order, greedily, preferring word starts.
    score, pos, first, last, run = 200, 0, -1, -1, 0
    for ch in low_term:
        found = -1
        # A word start for this letter at or after pos, else the next one.
        for j in range(pos, len(low)):
            if low[j] == ch and _boundary(text, j):
                found = j
                break
        nxt = low.find(ch, pos)
        if nxt < 0:
            return None
        if found < 0 or (nxt == last + 1):
            found = nxt
        if first < 0:
            first = found
        if found == last + 1:
            run += 1
            score += 12 * run
        else:
            run = 0
            score -= min(found - last - 1, 20) if last >= 0 else min(found, 20)
        if _boundary(text, found):
            score += 25
        last, pos = found, found + 1
    span = last - first + 1
    if span > max(3 * len(low_term), len(low_term) + 8):
        return None
    return score


def match_table(
    terms: list[str], name: str, columns: list[str], lowered: list[str] | None = None
) -> Match | None:
    """Whether every query word matches the table ``name`` or a column.

    A word that matches the name fuzzily scores by :func:`fuzzy_score`; one
    that only matches a column (contained, ``COLUMN_MIN`` letters or more)
    scores less, more for an exact column name, and is remembered so the list
    can say which column it was. ``lowered`` (``columns`` in lowercase,
    computed once by the caller) saves lowering 78,000 names per keystroke.
    """
    total, column = 0, ""
    for term in terms:
        score = fuzzy_score(term, name)
        if score is not None:
            total += score
            continue
        if len(term) < COLUMN_MIN:
            return None
        low = term.lower()
        if lowered is None:
            lowered = [c.lower() for c in columns]
        # One pass: an exact column name beats a prefix beats "contains".
        best, rank = -1, 0
        for i, col in enumerate(lowered):
            if low not in col:
                continue
            r = 3 if col == low else 2 if col.startswith(low) else 1
            if r > rank:
                best, rank = i, r
                if r == 3:
                    break
        if best < 0:
            return None
        total += 150 if rank == 3 else 100
        column = column or columns[best]
    return Match(total, column)
