"""Text that has to survive a UTF-8 boundary.

One rule, in one place, because the boundary is met twice: a provider's JSON
stream can hand this system a surrogate pair split across two escapes, and a
lone surrogate cannot be encoded as UTF-8 at all — so a store write or an MCP
result carrying one fails rather than delivering the answer it holds.
"""

from __future__ import annotations


def encodable(text: str) -> str:
    """Text as UTF-8 can carry it, with a split pair put back together.

    CPython's `json` does not combine `\\uXXXX` pairs: `json.loads` of a pair
    returns **two** surrogate characters.  That is how an emoji arrives from a
    provider, and there are two cases wanting opposite treatment: a *pair* is a
    real character split by the escape and wants putting back together; a
    *lone* surrogate is not a character at all, and U+FFFD is what every other
    decoder in the stack substitutes.

    The UTF-16 round trip does exactly that and nothing else: `surrogatepass`
    gets the surrogates out as bytes, and UTF-16's decoder recombines a valid
    pair and reports an unpaired one as a replacement.  Encoding to UTF-8 with
    `errors="replace"` is the tempting one-liner and is wrong — it turns the
    emoji into `??` and a lone surrogate into a question mark nobody can tell
    from a typed one.

    Safe on a JSON document as well as on plain text: a surrogate can only come
    out of `json.dumps` from inside a string literal, and U+FFFD is not one of
    JSON's metacharacters.
    """
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
