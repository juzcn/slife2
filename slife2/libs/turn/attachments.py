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
(`image_note`, which `slife2.libs.store.db`'s save path uses too), so a restored conversation
is text-only and a turn that needs the picture again attaches it again — the
tool's own answer in its history is what names the file to re-attach.

**And a turn that dies at the provider drops them.**  A picture a provider
refuses is refused again on every request that carries it, and a failed turn's
messages are re-sent by every turn that follows — so one bad attachment would
otherwise end the conversation, and in a server process that outlives the
terminal there would be no way back.  The repair is `drop`, and what it leaves is
exactly what a restore would have left: the live list and the stored row must
never disagree about whether a picture is still in hand.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import MutableSequence, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Suffix -> media type, for what a provider is told the bytes are.  A suffix
#: this table does not hold is **refused**, not guessed: the header decides how
#: the far end decodes the payload, so a wrong one is bytes nobody can read.
#: `media/adapters._data_uri` refuses an unknown suffix for the same question,
#: and two routes to a provider may not disagree about one thing — the earlier
#: guess here (`image/png` for an SVG or an ICO) is the disagreement this table
#: now closes.
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


def _encode(source: str) -> tuple[str, str]:
    """One source as `(url, refusal)` — exactly one of the two is filled in.

    **A `data:` URI and an `http(s)` URL pass straight through**, which is v1's
    rule and worth stating because it looks like a capability this process does
    not have: a remote URL is not fetched *here*.  It goes on the wire and the
    **provider** fetches it — OpenAI's `image_url` takes one, and Anthropic's
    `image` block has a `url` source — so nothing in this system makes a request
    anybody did not ask for.  (v1 did exactly this.)

    Anything else is a path on this machine: read, capped, and base64-encoded —
    with the media type its suffix names, or refused when the suffix names none
    this build knows (`MEDIA_TYPES`).  The refusal is the same rule
    `media/adapters._data_uri` applies to the same question: a guessed type is
    bytes the far end cannot decode, and the two routes must not disagree.

    **The reason comes back with the failure, and that is the point of the
    pair.**  This used to answer `""` and leave `resolve` to work out *why* from
    a second `is_file` and a second `stat` — two policies about one file, and the
    one that produced the URL was not the one that explained it.  A failure is
    still a value rather than an exception, because one unreadable source among
    five must not cost the other four: v1 learned that by dropping a whole batch
    on the first missing file.
    """
    if source.startswith(("data:", "http://", "https://")):
        return source, ""
    path = Path(source).expanduser()
    if not path.is_file():
        return "", f"{source}: no such file"
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "")
    if not media_type:
        return "", (
            f"{source}: {path.suffix or '(no suffix)'} is not an image format "
            f"this knows ({', '.join(sorted(MEDIA_TYPES))}) — the bytes would "
            f"arrive at the provider under a guessed type it cannot decode"
        )
    # **Asked of the file before it is read**, not of the bytes afterwards: an
    # oversized source is refused *without* allocating it, which matters because
    # the cap exists precisely for the file nobody should be holding in memory.
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.debug("attachment %s could not be stat'd: %s", path, exc)
        return "", f"{source}: could not be read"
    if size > MAX_BYTES:
        logger.warning("attachment %s is %d bytes, over the cap", path, size)
        return "", (
            f"{source}: {size / 1_048_576:.1f} MB, over the "
            f"{MAX_BYTES // 1_048_576} MB limit"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.debug("attachment %s could not be read: %s", path, exc)
        return "", f"{source}: could not be read"
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}", ""


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
        url, refusal = _encode(source)
        if refusal:
            refusals.append(refusal)
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
        built += [_image_part(url) for url in images]
        if isinstance(message, dict):
            message["content"] = built
        else:
            message.content = built
        return True
    return False


def _image_part(url: str) -> dict[str, Any]:
    """One image, as the wire spells it.

    **One site, because the module's whole argument is that the two routes must
    not come to different conclusions.**  `inject` and `parts` are the two ways
    a picture reaches a conversation — a tool adding one mid-turn and a caller
    naming one up front — and between them this literal was written twice.  It
    is a wire shape rather than a policy, so the two would not have disagreed
    about anything yet; the next field that joins it is where they would.
    """
    return {"type": "image_url", "image_url": {"url": url}}


def parts(prompt: str, urls: Sequence[str]) -> list[dict[str, Any]]:
    """The user's message as content parts: their text, then the images.

    Built before the turn starts and handed to the loop as the message itself,
    which is what the *caller's* `images=` needs — a source resolved up front
    rather than by a tool.  The tool's route uses `inject` instead, because by
    then the message is already in the list.
    """
    return [
        {"type": "text", "text": prompt},
        *(_image_part(url) for url in urls),
    ]


def image_note(part: dict[str, Any]) -> str:
    """What one image part leaves behind when the pixels are not kept.

    **One function for both keepers**, because the sentence is model-facing text
    twice over: a restored turn reads it, and — after `drop` — the live
    conversation reads it.  Two spellings would be two claims about one
    attachment, and the model has no way to tell which of them is the lie.

    The size is derived from the payload rather than stored: base64 carries four
    characters per three bytes, so the arithmetic is exact enough for a reader
    deciding whether to ask for the picture again.

    **A type it cannot read is named, not guessed.**  A `data:` URL names its
    media type in the header, and a remote URL is named by itself; a part whose
    source is neither — one the record kept without an address, or a `data:` URL
    with no type — was rendered as a bare `image`, a claim about a picture this
    function never had, in the one place a restored turn reads back (DESIGN.md §11.1).  It
    says the source is unnamed instead.
    """
    url = part.get("image_url")
    url = url.get("url") if isinstance(url, dict) else None
    media_type, _, payload = str(url or "").partition(";base64,")
    media_type = media_type.removeprefix("data:") or "an unnamed source"
    size = f", ~{len(payload) * 3 // 4 // 1024} KB" if payload else ""
    return (
        f"[image not stored: {media_type}{size} — the prompt names the file, "
        f"and attaching it again sends it]"
    )


#: What a *person* is told when `drop` has just taken their picture away, as
#: opposed to what the model reads where the picture was (`image_note`).
#:
#: Two audiences and so two sentences, which is not the two spellings
#: `image_note` exists to prevent: that rule is about one attachment described
#: twice *to the model*.  This one is an error line, and it has to say what the
#: note cannot — that something went wrong, and that nothing is broken.
DROPPED_IMAGES = (
    "The turn failed, so the attached image(s) were dropped from the "
    "conversation — a picture the provider refuses is refused by every turn "
    "after it. Naming the file again in a new message attaches it again."
)


def drop(messages: MutableSequence[Any]) -> int:
    """Replace every attached image in *messages* with a note.  `how many went`.

    **In place, which is the caller's need rather than a convenience.**  This runs
    when a turn has died at the provider, and what it repairs is the live list the
    *next* turn runs on — a copy would leave the poisoned parts exactly where they
    were.  `slife2.libs.store.db.strip_images` answers the same question for a list on its way
    to disk and it copies instead, because its caller's conversation may still be
    reasoning about the picture; that is the whole of the difference between them,
    and the note they leave is `image_note`.

    **The count rather than a boolean, because it is what the caller has to say.**
    A turn that failed with a picture in the conversation has been repaired, and
    the person waiting on it is owed that fact; a turn that failed with no picture
    anywhere is an ordinary failure, and a sentence about images there would be
    noise about something that did not happen.

    **Whole messages, not the tail.**  The caller hands the conversation rather
    than the turn, because a picture an earlier turn already got an answer about
    is still re-sent by every request — and because what this produces is what a
    restore would have produced, where no turn keeps one.  The cost is that the
    model cannot look at that older picture again without being given it again,
    which is the same cost restoring has always had.

    Only parts can hold an image, so a message whose content is still a plain
    string is left alone rather than rewritten into parts it does not need.
    """
    dropped = 0
    for message in messages:
        content = (
            message.get("content") if isinstance(message, dict) else message.content
        )
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                content[index] = {"type": "text", "text": image_note(part)}
                dropped += 1
    return dropped
