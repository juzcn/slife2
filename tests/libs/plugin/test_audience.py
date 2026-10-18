"""Who a call is for, and which home it is in.

Both travel out of band — beside the arguments rather than in them — because a
model must not be able to name somebody else's conversation, and must not be
able to name somebody else's directory either.  The home is the newer half and
it rides in the same blob, which is what makes it reach the far end of a chain
of proxies: `forwarded_client` carries the identity on, and the home is part of
it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from slife2.libs.host.paths import DATA_ENV_VAR, in_home
from slife2.libs.plugin.audience import (
    client_key,
    client_meta,
    client_of,
    foreign_home,
    forwarded_client,
    home_of,
    with_home,
)

pytestmark = pytest.mark.unit


def test_a_call_says_which_conversation_and_which_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home defaults to this process's, which is right at every call site.

    A client, a launcher and a test are one home from start to finish; a plugin
    making a call on somebody else's behalf is inside a call of its own, where
    this already answers with the caller's (`slife2.libs.plugin.protocol.serve`).
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "mine"))

    assert home_of(client_meta("jack", "worker")) == str(tmp_path / "mine")
    assert client_of(client_meta("jack", "worker")) == ("jack", "worker")
    assert home_of(client_meta("jack", home="/somewhere")) == "/somewhere"


def test_a_call_that_names_no_home_is_not_a_call_about_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`""` is the honest answer, and both readers treat it as "this process's".

    A `status`, a model server's listing, a probe: none of them is about a
    directory, and none of them may be read as naming one.
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path))

    assert home_of(None) == ""
    assert home_of({}) == ""
    assert home_of({"slife2/client": {"agent": "jack"}}) == ""
    assert home_of({"slife2/client": {"agent": "jack", "data_dir": 7}}) == ""
    assert client_of({"slife2/client": {"data_dir": "/somewhere"}}) is None


def test_the_home_goes_out_with_every_call_a_server_makes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`with_home`: filled in when the call says nothing, and never rewritten.

    The second half is the one that matters.  A home in a forwarded meta is the
    *caller's*, so a daemon serving two homes must not helpfully replace it with
    its own on the way past — that is the second hop of a chain writing into the
    wrong directory.
    """
    mine = tmp_path / "mine"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    theirs = {"slife2/client": {"agent": "jack", "data_dir": str(tmp_path / "theirs")}}

    assert home_of(with_home(None)) == str(mine)
    assert client_of(with_home({"protocol": "keys"})) is None, "no agent was named"
    assert home_of(with_home(theirs)) == str(tmp_path / "theirs"), "not rewritten"
    # And a proxy forwards the home on with the identity it forwards, which is
    # what makes it reach the far end of a chain rather than the first hop.
    assert home_of(forwarded_client(with_home(None))) == str(mine)


def test_a_relative_home_is_made_absolute_before_it_travels(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The far end resolves the home it is handed, and not from this directory.**

    Every daemon here is spawned with its working directory set to the machine's
    runtime directory (`slife2.libs.host.launcher._argv`), so a `--data-dir
    rel/chk` that travelled as it was typed would mean `<runtime>/rel/chk` at the
    other end: the client reads the config from the directory it named, the
    daemon writes `agents/jack` under the runtime directory, and nothing fails —
    the work is simply somewhere else.  `_argv` resolves the directory it puts in
    a child's argv for exactly this hazard; a `_meta` home is the same crossing
    by another road, and `with_home` fills in the same value for a call that
    names none.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(DATA_ENV_VAR, "rel/chk")

    sent = home_of(client_meta("jack"))

    assert Path(sent).is_absolute(), "a relative home is a different directory"
    assert sent == str((tmp_path / "rel" / "chk").resolve())
    assert home_of(with_home(None)) == sent, "the same answer for the bare case"


def test_a_home_spelled_on_another_platform_is_foreign(tmp_path: Path) -> None:
    """A home is absolute only in the spelling of the machine that wrote it.

    Mirrored WSL networking puts Windows and Linux on one loopback, so the same
    server receives `D:\\...` on POSIX and `/home/...` on Windows.  Both are
    names here, not roots, and resolving one as a relative path is the bug
    `foreign_home` exists to stop.
    """
    assert foreign_home("") == ""
    assert foreign_home(str(tmp_path)) == "", "this machine's own absolute path"

    foreign = (
        "/home/jack/.slife2" if sys.platform == "win32" else r"D:\Dev\Workspace\slife2"
    )
    assert foreign_home(foreign) == foreign


def test_one_conversation_is_keyed_by_the_three_things_that_identify_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**(home, agent, subagent)** — a name alone is half an identity here.

    Every plugin in this system is one process serving every agent, which is why
    each keys its state by an agent name at all; the moment there are two homes
    there are two agents called `jack`, and a table keyed by the name answers the
    second one with the first one's conversation.
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "mine"))

    assert client_key("jack") == (str(tmp_path / "mine"), "jack", "")
    with in_home(tmp_path / "theirs"):
        assert client_key("jack") == (str(tmp_path / "theirs"), "jack", "")
        assert client_key("jack", "worker")[2] == "worker"
