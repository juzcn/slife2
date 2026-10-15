"""Who a call is for, and which world it is in.

Both travel out of band — beside the arguments rather than in them — because a
model must not be able to name somebody else's conversation, and must not be
able to name somebody else's directory either.  The world is the newer half and
it rides in the same blob, which is what makes it reach the far end of a chain
of proxies: `forwarded_client` carries the identity on, and the world is part of
it.
"""

from __future__ import annotations

import pytest

from slife2.libs.host.paths import in_world
from slife2.libs.plugin.audience import (
    client_key,
    client_meta,
    client_of,
    forwarded_client,
    with_world,
    world_of,
)

pytestmark = pytest.mark.unit


def test_a_call_says_which_conversation_and_which_world(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The world defaults to this process's, which is right at every call site.

    A client, a launcher and a test are one world from start to finish; a plugin
    making a call on somebody else's behalf is inside a call of its own, where
    this already answers with the caller's (`slife2.libs.plugin.protocol.serve`).
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "mine"))

    assert world_of(client_meta("jack", "worker")) == str(tmp_path / "mine")
    assert client_of(client_meta("jack", "worker")) == ("jack", "worker")
    assert world_of(client_meta("jack", world="/somewhere")) == "/somewhere"


def test_a_call_that_names_no_world_is_not_a_call_about_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`""` is the honest answer, and both readers treat it as "this process's".

    A `status`, a model server's listing, a probe: none of them is about a
    directory, and none of them may be read as naming one.
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path))

    assert world_of(None) == ""
    assert world_of({}) == ""
    assert world_of({"slife2/client": {"agent": "jack"}}) == ""
    assert world_of({"slife2/client": {"agent": "jack", "world": 7}}) == ""
    assert client_of({"slife2/client": {"world": "/somewhere"}}) is None


def test_the_world_goes_out_with_every_call_a_server_makes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`with_world`: filled in when the call says nothing, and never rewritten.

    The second half is the one that matters.  A world in a forwarded meta is the
    *caller's*, so a daemon serving two worlds must not helpfully replace it with
    its own on the way past — that is the second hop of a chain writing into the
    wrong directory.
    """
    mine = tmp_path / "mine"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    theirs = {"slife2/client": {"agent": "jack", "world": str(tmp_path / "theirs")}}

    assert world_of(with_world(None)) == str(mine)
    assert client_of(with_world({"protocol": "keys"})) is None, "no agent was named"
    assert world_of(with_world(theirs)) == str(tmp_path / "theirs"), "not rewritten"
    # And a proxy forwards the world on with the identity it forwards, which is
    # what makes it reach the far end of a chain rather than the first hop.
    assert world_of(forwarded_client(with_world(None))) == str(mine)


def test_one_conversation_is_keyed_by_the_three_things_that_identify_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**(world, agent, subagent)** — a name alone is half an identity here.

    Every plugin in this system is one process serving every agent, which is why
    each keys its state by an agent name at all; the moment there are two worlds
    there are two agents called `jack`, and a table keyed by the name answers the
    second one with the first one's conversation.
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "mine"))

    assert client_key("jack") == (str(tmp_path / "mine"), "jack", "")
    with in_world(tmp_path / "theirs"):
        assert client_key("jack") == (str(tmp_path / "theirs"), "jack", "")
        assert client_key("jack", "worker")[2] == "worker"
