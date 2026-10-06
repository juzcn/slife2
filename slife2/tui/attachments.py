"""Reading `@path` images out of a prompt.

slife v1's convention, kept because it needs no UI: an image is named where it
belongs, in the sentence that refers to it — `look at @screenshot.png and tell
me what is wrong` — and the marker stays in the transcript, so the record shows
what was actually sent rather than a prompt with a hole in it.

Two things this deliberately does not do:

**It does not fetch URLs.**  `@https://…` is refused rather than downloaded.
Fetching an address a prompt named is a request nobody made, from a process
whose job is to read a file the user already has.

**It does not fail the prompt.**  A path that is not there, is too large, or is
not an image produces a complaint that the caller shows — and the text is still
sent.  Dropping the whole message because an attachment was wrong would lose
what the user typed.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: What an `@mention` may look like.  Stops at whitespace, which is what makes
#: the marker usable mid-sentence; a path with a space in it needs quoting and is
#: not supported, because guessing where it ends is worse than saying so.
_MENTION = re.compile(r"@(\S+)")

#: Suffix -> media type.  A closed set on purpose: the media type has to be
#: right, and guessing it from content is a dependency this does not need.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: How large an attachment may be.  Base64 inflates by a third and the whole
#: thing travels in one JSON-RPC message, so a ceiling somewhere is required;
#: this one is above any screenshot and below anything that would make a
#: request unreasonable.
MAX_BYTES = 10 * 1024 * 1024


def extract(text: str) -> tuple[list[str], list[str]]:
    """The images a prompt refers to, as `(data_urls, complaints)`.

    The text is left alone — the markers stay where the user put them.
    """
    urls: list[str] = []
    complaints: list[str] = []

    for mention in _MENTION.findall(text):
        if "://" in mention:
            complaints.append(f"@{mention}: only local files can be attached")
            continue

        path = Path(mention)
        media_type = MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            # Not every `@mention` is an attachment — `@channel` in a sentence
            # is just text — so an unknown suffix is passed over in silence.
            # Only something that *looks* like an image is worth complaining
            # about.
            if path.suffix.lower() in {".bmp", ".tiff", ".svg", ".heic"}:
                complaints.append(f"@{mention}: unsupported image format")
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            complaints.append(f"@{mention}: {exc.strerror or exc}")
            continue

        if len(data) > MAX_BYTES:
            complaints.append(
                f"@{mention}: {len(data) / 1_048_576:.1f} MB, over the "
                f"{MAX_BYTES // 1_048_576} MB limit"
            )
            continue

        encoded = base64.b64encode(data).decode("ascii")
        urls.append(f"data:{media_type};base64,{encoded}")

    return urls, complaints
