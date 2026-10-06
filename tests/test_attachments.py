"""`@path` attachments: what gets read, and what gets refused.

The interesting cases are the refusals.  An attachment that silently does not
arrive leaves somebody wondering why the model ignored what they sent, which is
a worse failure than being told.
"""

from __future__ import annotations

import base64

import pytest

from slife2.tui.attachments import MAX_BYTES, extract

pytestmark = pytest.mark.unit

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend"


def image(tmp_path, name: str = "shot.png", data: bytes = PNG):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_an_existing_image_becomes_a_data_url(tmp_path) -> None:
    path = image(tmp_path)
    urls, complaints = extract(f"look at @{path.as_posix()} please")

    assert complaints == []
    assert len(urls) == 1
    assert urls[0].startswith("data:image/png;base64,")
    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG


@pytest.mark.parametrize("name", ["a.jpg", "a.jpeg", "a.gif", "a.webp", "a.PNG"])
def test_the_media_type_comes_from_the_suffix(tmp_path, name: str) -> None:
    path = image(tmp_path, name)
    urls, _ = extract(f"@{path.as_posix()}")
    assert len(urls) == 1
    assert urls[0].startswith("data:")
    assert ";base64," in urls[0]


def test_a_missing_file_is_reported_not_skipped(tmp_path) -> None:
    urls, complaints = extract("look at @nope.png")
    assert urls == []
    assert len(complaints) == 1
    assert "nope.png" in complaints[0]


def test_a_remote_url_is_refused(tmp_path) -> None:
    """Fetching an address a prompt named is a request nobody made.

    The component's job is to read a file the user already has.
    """
    urls, complaints = extract("@https://example.test/a.png")
    assert urls == []
    assert "only local files" in complaints[0]


def test_an_oversized_image_is_refused(tmp_path) -> None:
    path = image(tmp_path, "big.png", b"x" * (MAX_BYTES + 1))
    urls, complaints = extract(f"@{path.as_posix()}")
    assert urls == []
    assert "over the" in complaints[0]


def test_a_mention_that_is_not_an_image_is_passed_over() -> None:
    """`@channel` in a sentence is text, and complaining would be noise."""
    assert extract("ask @channel about it") == ([], [])


def test_an_unsupported_image_format_is_reported() -> None:
    """...but something that *looks* like an image is worth saying no to."""
    urls, complaints = extract("@diagram.svg")
    assert urls == []
    assert "unsupported" in complaints[0]


def test_the_text_is_left_alone(tmp_path) -> None:
    """The marker stays where the user put it.

    The transcript should show what was sent, and taking the marker out would
    leave a sentence with a hole where the attachment was named.
    """
    path = image(tmp_path)
    text = f"look at @{path.as_posix()} and tell me"
    extract(text)
    assert f"@{path.as_posix()}" in text


def test_several_images_and_one_bad_path(tmp_path) -> None:
    first = image(tmp_path, "one.png")
    second = image(tmp_path, "two.png")
    urls, complaints = extract(f"@{first.as_posix()} @missing.png @{second.as_posix()}")
    # The good ones still go, and the prompt is not lost over the bad one.
    assert len(urls) == 2
    assert len(complaints) == 1
