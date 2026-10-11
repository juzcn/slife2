"""Attaching images to a turn: three kinds of source, and where they land.

An image reaches the model one way and one way only — `attach_image`, the tool
the model calls for itself:

    看看 D:\\shots\\a.png 上面有什么

is a path in a sentence, and the model attaching it is how it comes to see it.
There is no marker syntax and no harness call; a person writes what they mean and
a model does the one thing a person cannot do for it.

**Three kinds of source**, and only the first is read here:

* a **local path** — read, capped, base64-encoded into a `data:` URL;
* an **`http(s)` URL** — passed on untouched, because the *provider* fetches it
  (OpenAI's `image_url` takes one and Anthropic's `image` block has a `url`
  source), so nothing in this system makes a request anybody did not ask for;
* a **`data:` URI** — already what a provider wants, passed on.

**A source that cannot be read is a refusal, not a failure of the batch.**  One
missing file among five must not cost the other four — v1 learned that the hard
way — so `resolve` answers with what it attached and what it did not, and the
tool's answer says both.  A refusal that nobody is told about is the outcome
worse than either.

**Where the pixels go, and how long they live.**  They are content parts on the
**user message** — the only place a wire here can carry an image, since a tool
result is text on every backend this system speaks.  They are live-session state
and nothing more: what is stored is the text plus a note naming every file
(`slife2.db`'s `strip_images`), so a restored conversation is text-only and a
turn that needs the picture again attaches it again — the tool's own answer in
its history is what names the file to re-attach.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import MutableSequence, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Suffix -> media type, for what a provider is told the bytes are.  Only the
#: common raster formats carry their real one: an SVG or an ICO is sent as
#: `image/png` because that is the guess a vision API accepts, and v1 made the
#: same trade — a wrong-but-plausible type is a 400 the caller can read, while a
#: right-and-unaccepted one is a 400 with no way forward.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: How large a local file may be.  Base64 inflates by a third and the whole thing
#: travels in one JSON-RPC message, so a ceiling is required; this one is above
#: any screenshot and below anything that makes a request unreasonable — v1's
#: number, arrived at against the same two bounds.
MAX_BYTES = 20 * 1024 * 1024


def as_url(source: str) -> str:
    """One source, as the URL a provider is given — or `""`.

    **A `data:` URI and an `http(s)` URL pass straight through**, which is v1's
    rule and worth stating because it looks like a capability this process does
    not have: a remote URL is not fetched *here*.  It goes on the wire and the
    **provider** fetches it — OpenAI's `image_url` takes one, and Anthropic's
    `image` block has a `url` source — so nothing in this system makes a request
    anybody did not ask for.  (v1 did exactly this.)

    Anything else is a path on this machine: read, capped, and base64-encoded.

    **A failure is `""` rather than an exception**, and the caller turns it into a
    sentence for whoever typed the `@`: one unreadable source among five must not
    cost the other four, which v1 learned by dropping a whole batch on the first
    missing file.
    """
    if source.startswith(("data:", "http://", "https://")):
        return source
    path = Path(source).expanduser()
    if not path.is_file():
        return ""
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "image/png")
    # **Asked of the file before it is read**, not of the bytes afterwards: an
    # oversized source is refused *without* allocating it, which matters because
    # the cap exists precisely for the file nobody should be holding in memory.
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.debug("attachment %s could not be stat'd: %s", path, exc)
        return ""
    if size > MAX_BYTES:
        logger.warning("attachment %s is %d bytes, over the cap", path, size)
        return ""
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.debug("attachment %s could not be read: %s", path, exc)
        return ""
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def resolve(sources: Sequence[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Those sources, as `([(source, data_url)], refusals)` — one bad one costs
    only itself.

    The source travels back with its encoded form so that a caller can *name*
    what it attached: a data URL is megabytes and is never printed, while the
    answer the model reads has to say which files it was shown — which is also
    what its own history keeps after the pixels are gone.

    **The one place a source becomes bytes**, so the tool the model calls and the
    markers a prompt carries cannot come to different conclusions about the same
    file.  v1 learned the batch rule the hard way: its first version dropped the
    whole list on one unreadable source, so four good images went with the
    missing fifth.
    """
    pairs: list[tuple[str, str]] = []
    refusals: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if source in seen:
            # Named twice, attached once: the second copy is the same bytes in
            # the same request, and a screenshot is megabytes the model does not
            # read twice.
            continue
        seen.add(source)
        url = as_url(source)
        if not url:
            path = Path(source).expanduser()
            if not path.is_file():
                refusals.append(f"{source}: no such file")
            elif path.stat().st_size > MAX_BYTES:
                size = path.stat().st_size
                refusals.append(
                    f"{source}: {size / 1_048_576:.1f} MB, over the "
                    f"{MAX_BYTES // 1_048_576} MB limit"
                )
            else:
                refusals.append(f"{source}: could not be read")
            continue
        pairs.append((source, url))
    return pairs, refusals


def inject(messages: MutableSequence[Any], images: Sequence[str]) -> bool:
    """Put the images on the **last user message** in *messages*.  `any placed`.

    The last user message and not the newest message: by the time this runs the
    loop has appended the person's message and nothing else, but on a later turn
    it may have appended a whole exchange — and what is being attached is always
    what somebody said, never a tool result.  v1's `inject_images_to_last_user`,
    and the search backwards is the same one and for the same reason.

    A message whose content is already parts is extended; a string becomes text
    parts first.  **Idempotent by construction**: a second call with the same
    images appends them again, which is why the caller does this once per turn
    (`attach_image` is asked for once) rather than being relied on not to.

    Returns whether anything was placed, so a caller can tell "attached" from
    "there was no user message to attach it to" — the second being a bug in the
    caller rather than something to report to a person.
    """
    if not images:
        return False
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else message.role
        if role != "user":
            continue
        content = (
            message.get("content") if isinstance(message, dict) else message.content
        )
        built = (
            [{"type": "text", "text": content}]
            if isinstance(content, str)
            else list(content or [])
        )
        built += [{"type": "image_url", "image_url": {"url": url}} for url in images]
        if isinstance(message, dict):
            message["content"] = built
        else:
            message.content = built
        return True
    return False


def parts(prompt: str, urls: Sequence[str]) -> list[dict[str, Any]]:
    """The user's message as content parts: their text, then the images.

    Built before the turn starts and handed to the loop as the message itself,
    which is what the *caller's* `images=` needs — a source resolved up front
    rather than by a tool.  The tool's route uses `inject` instead, because by
    then the message is already in the list.
    """
    return [
        {"type": "text", "text": prompt},
        *({"type": "image_url", "image_url": {"url": url}} for url in urls),
    ]
