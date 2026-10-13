"""Attachment sources: what is read, what is passed on, and where it lands.

Three kinds of source — a local path, an `http(s)` URL and a `data:` URI — and
only the first is read here: the other two go to the provider as they are,
because nothing in this system fetches an address a model or a person named.

The interesting cases are the refusals.  An attachment that silently does not
arrive leaves somebody wondering why the model ignored what they sent, which is a
worse failure than being told — and one missing file must not cost the others.
"""

from __future__ import annotations

import base64

import pytest

from slife2 import attachments
from slife2.messages import Message

pytestmark = pytest.mark.unit

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend"


def image(tmp_path, name: str = "shot.png", data: bytes = PNG):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- what a source becomes ----------------------------------------------------


def test_a_local_file_becomes_a_data_url_naming_its_type(tmp_path) -> None:
    path = image(tmp_path)
    pairs, refusals = attachments.resolve([path.as_posix()])

    assert refusals == []
    assert pairs[0][0] == path.as_posix()
    assert pairs[0][1].startswith("data:image/png;base64,")
    assert base64.b64decode(pairs[0][1].split(",", 1)[1]) == PNG


def test_a_url_and_a_data_uri_are_passed_through_untouched(tmp_path) -> None:
    """**Nothing here fetches.**  A provider does, on its own side.

    Both wires this system speaks accept a remote image URL, and neither asks
    this process to have downloaded it — so a prompt cannot aim a request at this
    machine by naming an address.
    """
    data = "data:image/png;base64,AAAA"
    url = "https://x.test/a.png"
    pairs, refusals = attachments.resolve([url, data])

    assert refusals == []
    assert pairs == [(url, url), (data, data)]


def test_one_unreadable_source_does_not_cost_the_others(tmp_path) -> None:
    """v1's batch rule, learned the hard way: four good images went with the
    missing fifth because the first version dropped the whole list."""
    present = image(tmp_path)
    pairs, refusals = attachments.resolve(
        [present.as_posix(), str(tmp_path / "gone.png"), "https://x.test/b.png"]
    )

    assert [source for source, _ in pairs] == [
        present.as_posix(),
        "https://x.test/b.png",
    ]
    assert len(refusals) == 1
    assert "no such file" in refusals[0]
    assert "gone.png" in refusals[0]


def test_an_oversized_file_is_refused_by_its_size(tmp_path, monkeypatch) -> None:
    """A cap, and a refusal that says which number was too big."""
    monkeypatch.setattr(attachments, "MAX_BYTES", 8)
    path = image(tmp_path, data=b"1234567890")

    pairs, refusals = attachments.resolve([path.as_posix()])

    assert pairs == []
    assert "over the" in refusals[0] and "0.0 MB" in refusals[0]


def test_a_source_named_twice_is_attached_once(tmp_path) -> None:
    """A screenshot is megabytes the model does not read twice."""
    path = image(tmp_path)
    pairs, _ = attachments.resolve([path.as_posix(), path.as_posix()])

    assert len(pairs) == 1


def test_an_unknown_suffix_is_refused_rather_than_guessed(tmp_path) -> None:
    """The header decides how the far end decodes the bytes.

    An SVG was sent as `image/png` here while `media/adapters._data_uri`
    *refused* an unknown suffix for the same question — two routes to a provider
    disagreeing about one thing.  Refusing is the one rule both follow now: a
    guessed type is bytes the model cannot read, and the refusal names the
    suffix.
    """
    path = image(tmp_path, name="diagram.svg", data=b"<svg/>")
    pairs, refusals = attachments.resolve([path.as_posix()])

    assert pairs == []
    assert len(refusals) == 1
    assert "diagram.svg" in refusals[0]
    assert ".svg" in refusals[0]
    assert "image/png" not in refusals[0]


# --- where it lands -----------------------------------------------------------


def test_the_images_go_on_the_last_user_message() -> None:
    """Content parts on the user message: text first, then the images.

    **The only place a wire here can carry one.**  A tool result is text on every
    backend this system speaks, which is why v1's `attach_image` injected into
    the user message too rather than answering with the picture.
    """
    messages: list[Message] = [
        Message(role="user", content="earlier"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="看看这个"),
    ]

    assert attachments.inject(messages, ["data:image/png;base64,AAAA"]) is True

    assert messages[2].content == [
        {"type": "text", "text": "看看这个"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert messages[0].content == "earlier", "an older message is left alone"


def test_the_caller_route_builds_the_same_parts_the_tool_injects() -> None:
    """`images=` and `attach_image` reach the model the same way.

    One encoder for both, which is the point of the module: a caller that holds a
    path and a model that is given one must not come to different conclusions
    about the same file.  `parts` is the caller's half — the message is built
    with its images before the turn starts — and `inject` is the tool's, because
    by then the message is already in the list.
    """
    built = attachments.parts("看看这个", ["data:image/png;base64,AAAA"])

    assert built == [
        {"type": "text", "text": "看看这个"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def test_nothing_to_attach_to_is_answered_rather_than_raised() -> None:
    assert (
        attachments.inject([Message(role="assistant", content="hi")], ["data:x"])
        is False
    )
    assert attachments.inject([], ["data:x"]) is False
    assert attachments.inject([Message(role="user", content="hi")], []) is False


# --- taking them back off -----------------------------------------------------


def test_dropping_an_image_leaves_a_note_and_says_how_many_went() -> None:
    """The repair a turn that died at the provider needs, and it is in place.

    In place because what it repairs is the live list the *next* turn runs on:
    a copy would leave the refused parts exactly where they were, which is the
    state this exists to end.  The count is the answer because it is what a
    caller has to say out loud — a failure with a picture in it is not an
    ordinary failure.
    """
    messages: list[Message] = [
        Message(role="user", content="earlier"),
        Message(
            role="user",
            content=attachments.parts(
                "看看这个", ["data:image/png;base64," + "A" * 4000]
            ),
        ),
    ]

    assert attachments.drop(messages) == 1
    assert messages[0].content == "earlier", "a message with no image is untouched"
    assert messages[1].content[0] == {"type": "text", "text": "看看这个"}
    note = messages[1].content[1]
    assert note["type"] == "text"
    assert "image/png" in note["text"] and "not stored" in note["text"]
    assert "~2 KB" in note["text"], "the size is what a reader decides on"
    assert "A" * 100 not in note["text"], "the payload does not survive as text"
    assert attachments.drop(messages) == 0, "there is nothing left to drop"


def test_the_drop_understands_the_wire_shape_too() -> None:
    """`inject` duck-types a dict and a `Message`; so does the drop.

    Both shapes are real here: the live conversation is `Message` objects and
    what a store hands back is the wire's dicts.
    """
    wire = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}
            ],
        }
    ]

    assert attachments.drop(wire) == 1
    assert wire[0]["content"][0]["text"].startswith("[image not stored: image/jpeg")


def test_the_note_names_the_source_it_knows() -> None:
    """The note is what the *record* keeps, so it must not invent a media type.

    A `data:` URL names its type in the header and a remote URL is named by
    itself, but a part with no readable source was rendered as a bare `image` —
    a claim about a picture this function never saw, in the one sentence a
    restored turn reads back (§10.1).  What it knows, it names.
    """
    typed = attachments.image_note(
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    )
    assert "image/png" in typed

    remote = attachments.image_note(
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}
    )
    assert "https://x.test/a.png" in remote

    unnamed = attachments.image_note({"type": "image_url", "image_url": {}})
    assert "image not stored: an unnamed source" in unnamed
