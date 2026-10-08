"""How a turn becomes searchable text, and how a query becomes a MATCH.

The keyword leg of the search is one FTS5 table over one derived string per
turn, and a query has to be built to match it.  Both halves are one grammar, so
both live here — for the reason `slife2.timeutil` is one module: two spellings
of "what a term is" is how the index and the query stop agreeing.

**CJK is split into single characters, and the query is split the same way.**
An FTS5 tokenizer sees a contiguous Chinese run as *one* token, so a
two-character query is an exact-token lookup and misses the word wherever it
sits inside a longer run.  Measured on this repository's own turn log, a
two-character word matched only the turns where it happened to sit beside
punctuation or a digit; with the characters separated it matched all of them.
Text whose CJK characters are space-separated makes every character a token, a
query mirrors it, and a CJK run becomes an FTS5 *phrase* — whose token
positions give back exactly the adjacency that splitting the characters took
away.  A Latin word needs none of that: it is one token already, so it is
quoted as the word it is, and the cut between the two scripts is where a term
ends (`_script_runs`).

Measured against `unicode61`: `工`, `工具`, `搜索一下` and whole English words
all match; `具我` matches and `我工` does not, so adjacency is real rather than
"the characters are somewhere in the row".  That is bigram-grade precision and
single characters still work, which a bigram tokenizer would not give — and
SQLite has no bigram tokenizer to give it.  Its one loss is English prefixes
(`tru` does not match `trump`), which a search term written as a whole word
does not miss.

**Terms are ANDed, never ORed.**  With single-character tokens an OR matches
any turn holding any one of the characters, which on a Chinese turn log is
every turn — measured, an OR for `计算 792` returned a turn whose text is a
tool catalogue, because a catalogue contains every word.  The cost is that the
keyword leg fires only on a turn holding *every* term, and Chinese terms are
matched as the literal phrase they were written as.  That is the honest trade:
this leg is the precision one, and the semantic leg carries recall — measured,
four of twelve realistic queries for this turn log match no keyword at all.

**Nothing here is a LIKE pattern, and nothing is interpolated.**  Every term is
quoted whole, so `AND`, `*`, `NEAR`, `(` and a stray quote arriving from a
model are characters rather than FTS5 query syntax.  A query carrying no term
is refused rather than handed to the parser, whose empty pattern matches
everything.
"""

from __future__ import annotations

import re

#: The rule set's own version.  It is recorded beside the indexes, because an
#: index built by one version of `normalize` cannot be searched by another —
#: the version is how the index learns that it has to be rebuilt.  Bump it with
#: any change to what `normalize` or `terms` produces.
RULES_VERSION = "1"

#: CJK Unified Ideographs and its Extension A — the two ranges v1 split on —
#: widened to the scripts that are also written without spaces between words,
#: which would otherwise become one token each.  Deliberately a fixed table
#: rather than a Unicode property lookup: this decides where spaces go in a
#: string an index is built from, so it may not change with the runtime's
#: Unicode data.
_CJK_CLASS = (
    "぀-ヿ"  # hiragana, katakana
    "㐀-䶿"  # CJK Unified Ideographs Extension A
    "一-鿿"  # CJK Unified Ideographs
    "豈-﫿"  # CJK Compatibility Ideographs
    "가-힯"  # hangul syllables
)

_CJK_RE = re.compile(f"[{_CJK_CLASS}]")

#: A run of them, which is what a term is cut into at the script boundary —
#: see `_script_runs` for why the two scripts want opposite treatment.
_CJK_RUN_RE = re.compile(f"[{_CJK_CLASS}]+")

#: What separates one term from the next: whitespace, ASCII punctuation, and
#: the full-width and CJK forms a Chinese sentence is punctuated with.  A
#: character kept out of this set stays inside a term, so an underscore does —
#: it is a token character to `unicode61`, and a term holding one is one word.
_SEPARATOR_RE = re.compile(
    "[\u0000-/:-@[-`{-¿"
    "‐-‧"  # dashes, quotes, ellipsis
    "　-〿"  # 、。〈〉《》「」『』【】
    "！-／：-＠［-｀｛-･"
    "]+"
)

_WHITESPACE_RE = re.compile(r"\s+")


class EmptyQuery(ValueError):
    """A query with no term in it — nothing to match, so nothing was asked."""


def normalize(text: str) -> str:
    """The one normalization, applied to a turn's text and to a query alike.

    Each CJK character gets a space on either side and the whole is
    case-folded, which is what makes a character an FTS5 token and what makes
    the two sides comparable string for string.  Runs of whitespace collapse,
    so a caller's formatting cannot change what a phrase matches.
    """
    spaced = _CJK_RE.sub(lambda found: f" {found.group(0)} ", text)
    return _WHITESPACE_RE.sub(" ", spaced).strip().casefold()


def _script_runs(piece: str) -> list[str]:
    """One piece cut where the script changes — CJK beside anything else.

    The two scripts want opposite things from a term.  A Latin word is already
    one token, so quoting it asks for that word.  A CJK run is *not* one token —
    its characters are indexed one by one — so the run has to stay together, to
    be built into the phrase that holds it.  Cutting at the boundary is what
    lets a query that glued the two together ask for the word and the word:
    `google搜索` becomes the two terms `google` and `搜索`, not one term
    demanding that they be exactly adjacent.
    """
    runs: list[str] = []
    position = 0
    for found in _CJK_RUN_RE.finditer(piece):
        if found.start() > position:
            runs.append(piece[position : found.start()])
        runs.append(found.group(0))
        position = found.end()
    if position < len(piece):
        runs.append(piece[position:])
    return runs


def terms(query: str) -> list[str]:
    """A query split into terms — normalized, in the order written, unduplicated.

    Split on punctuation *before* normalizing, not after: normalization puts a
    space between every pair of CJK characters, so splitting on whitespace
    afterwards would take a Chinese word apart into the single characters whose
    adjacency is the only thing making it a word.  A repeated term is dropped
    because ANDing it again asks the same question twice.
    """
    found: list[str] = []
    for piece in _SEPARATOR_RE.split(query):
        for run in _script_runs(piece):
            normalized = normalize(run)
            if normalized and normalized not in found:
                found.append(normalized)
    return found


def match_expression(query: str) -> str:
    """The FTS5 `MATCH` expression for a query: every term, quoted, ANDed.

    Raises:
        EmptyQuery: If the query holds no term at all.  Refused rather than
            answered: an empty `MATCH` pattern matches every row, so accepting
            one would make search an accidental browse, and browsing is what
            `turn_list` is for.
    """
    found = terms(query)
    if not found:
        raise EmptyQuery(f"no search term in {query!r}")
    return " AND ".join(f'"{term}"' for term in found)
