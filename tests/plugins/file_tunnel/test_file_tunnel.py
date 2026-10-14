"""`slife2-file-tunnel`: the registry, the two tools, and the one route.

Three halves, each driven where it can be reached without a tunnel.

The *tools* run over the in-memory transport with a fake tunnel injected, so
what is under test is which file, which token and what the far end is told —
v1's tests patched a module global to do this; an injected provider says the
same thing as a contract.  The *route* is driven through the real ASGI app
(`FastMCP.http_app()`), which is the half worth being hardest about: it is the
one thing about this plugin that is new to slife2, and a URL handed to a vision
model is worthless if `/share/<token>` does not answer.  The *guards* — the
credential denylist, the stat pin, the RFC 5987 filename — are v1's, and each
one was a bug before it was a test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import FileTunnelSettings, load
from slife2.plugins.audience import client_meta, for_the_model
from slife2.plugins.file_tunnel.providers import DEFAULT_PROVIDER, KNOWN_PROVIDERS
from slife2.plugins.file_tunnel.server import (
    Exposed,
    Exposure,
    Revoked,
    _ensure,
    build_server,
    content_disposition,
    is_credential_path,
    serve_exposed,
)

pytestmark = pytest.mark.unit

#: The checkout, found by the file that names it rather than by counting
#: directories up: a depth is a number that goes stale in silence.
REPO = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)


class FakeTunnel:
    """A tunnel that reports what a test tells it to, and records the calls.

    The tool half is about *addressing* — is there a URL, would it be served,
    what does the share URL look like — so a double that answers exactly those
    questions is what makes the assertions readable.
    """

    label = "fake"

    def __init__(
        self,
        *,
        url: str = "https://tunnel.test",
        active: bool = True,
        reachable: bool = True,
        can_start: bool = True,
        state: str | None = None,
        reason: str = "",
        vanishes: bool = False,
    ) -> None:
        self.url = url
        self.active = active
        self.reachable = reachable
        self.can_start = can_start
        self.state = state
        self.reason = reason
        #: Reports a URL, then cannot build one — the tunnel dying between the
        #: reachability check and the call that hands the link over.
        self.vanishes = vanishes
        self.started: list[int] = []
        self.monitors: list[int] = []
        self.stops = 0
        self.edge_ip = ""
        self.edge_via_proxy = False
        self.binary_path = ""

    @property
    def is_active(self) -> bool:
        return self.active

    @property
    def is_starting(self) -> bool:
        return False

    def is_reachable(self) -> bool:
        return self.reachable and self.active

    def share_url_for(self, token: str) -> str | None:
        if self.vanishes:
            return None
        return f"{self.url}/share/{token}" if self.active else None

    def status(self) -> dict[str, str]:
        if self.state is not None:
            found: dict[str, str] = {"state": self.state, "url": ""}
            if self.reason:
                found["reason"] = self.reason
            return found
        if self.active:
            return {"state": "active", "url": self.url}
        return {"state": "idle", "url": ""}

    def start(self, port: int) -> str:
        self.started.append(port)
        if not self.can_start:
            raise RuntimeError("no tunnel today")
        self.active = True
        return self.url

    def stop(self) -> None:
        self.stops += 1
        self.active = False

    def start_monitor(self, port: int) -> None:
        self.monitors.append(port)

    def stop_monitor(self) -> None:
        return None


class SlowStartTunnel(FakeTunnel):
    """A tunnel whose eager start is still in flight when the first call lands.

    That is the ordinary case rather than a corner: the eager start is a task of
    its own (`build_server`'s lifespan), so a first tool call routinely arrives
    while it is running — and that window is the whole reason `_ensure` exists.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("active", False)
        super().__init__(**kwargs)
        self._in_flight = True

    @property
    def is_starting(self) -> bool:
        return self._in_flight

    def finish(self) -> None:
        """The eager start concludes: a URL published, no attempt in flight."""
        self._in_flight = False
        self.active = True


def server_for(tunnel: FakeTunnel | None = None) -> FastMCP:
    """The plugin over the default config, with one fake tunnel."""
    return build_server(load(), provider=tunnel or FakeTunnel())


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(
        tool, arguments, meta=client_meta("jack"), raise_on_error=False
    )
    return "".join(getattr(block, "text", "") for block in result.content or [])


async def fetch(app: Any, path: str) -> httpx2.Response:
    """One GET against the real ASGI app — no socket, no tunnel."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:
        return await http.get(path)


def exposed_url(answer: str) -> str:
    """The URL out of what `expose_local_image` says."""
    return answer.splitlines()[1].strip()


# --- the surface --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_tools_are_the_models_and_the_third_is_not() -> None:
    """The plugins's own tools arrive by `tools/list`, and the audience mark is
    the only thing that decides which reach a model.  `tunnel_status` is the
    diagnosis the tunnel otherwise has no way to report, and it must never be
    something the model calls."""
    async with Client(server_for()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {
        "expose_local_image",
        "unexpose_local_image",
        "tunnel_status",
    }
    assert for_the_model(listed["expose_local_image"].meta)
    assert for_the_model(listed["unexpose_local_image"].meta)
    assert not for_the_model(listed["tunnel_status"].meta)


# --- exposing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_exposing_answers_with_a_public_url(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")

    async with Client(server_for()) as client:
        answer = await ask(client, "expose_local_image", path=str(picture))

    assert "photo.png" in answer
    assert exposed_url(answer).startswith("https://tunnel.test/share/")
    assert len(exposed_url(answer).rsplit("/", 1)[-1]) == 30


@pytest.mark.asyncio
async def test_the_same_file_twice_is_the_same_link(tmp_path: Path) -> None:
    """A second call for an unchanged file hands back the link already out
    there — minting another would leave the first one serving the same bytes
    under a URL nobody was told about."""
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")

    async with Client(server_for()) as client:
        first = await ask(client, "expose_local_image", path=str(picture))
        second = await ask(client, "expose_local_image", path=str(picture))

    assert exposed_url(first) == exposed_url(second)


@pytest.mark.asyncio
async def test_a_changed_file_gets_a_new_link(tmp_path: Path) -> None:
    """The pin is the point: re-exposing must not hand back the URL that now
    answers 403 to everyone holding it."""
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"one")
    async with Client(server_for()) as client:
        first = await ask(client, "expose_local_image", path=str(picture))
        picture.write_bytes(b"two, and longer")
        second = await ask(client, "expose_local_image", path=str(picture))

    assert exposed_url(first) != exposed_url(second)


@pytest.mark.asyncio
async def test_a_missing_file_is_refused() -> None:
    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="no file at"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(Path("nowhere") / "absent.png")},
                meta=client_meta("jack"),
            )


@pytest.mark.asyncio
async def test_a_directory_is_refused(tmp_path: Path) -> None:
    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="directory, not a file"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(tmp_path)},
                meta=client_meta("jack"),
            )


@pytest.mark.asyncio
async def test_a_private_credential_is_refused(tmp_path: Path) -> None:
    """A share URL is public while the tunnel is up, so publishing a key is
    publishing it."""
    secret = tmp_path / "id_ed25519"
    secret.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----")

    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="private credential"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(secret)},
                meta=client_meta("jack"),
            )


@pytest.mark.asyncio
async def test_a_dotenv_is_refused(tmp_path: Path) -> None:
    env = tmp_path / ".env.production"
    env.write_bytes(b"API_KEY=secret")

    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="private credential"):
            await client.call_tool(
                "expose_local_image", {"path": str(env)}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_the_credential_guard_reads_the_path_that_will_be_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harmless name must not launder the file it points at.

    `expose_local_image` registers and serves `str(target.resolve())`, so a
    symlink (`photo.png` → `~/.ssh/id_rsa`) sailed past a denylist check made
    on the name as given and handed the private key out under a share URL —
    exactly what the guard promises cannot happen.  The symlink is simulated by
    making `resolve()` answer with the credential (the files themselves are
    real) because creating one needs a privilege Windows does not grant by
    default, and *which path the guard reads* is the thing that was wrong.
    """
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    decoy = tmp_path / "photo.png"
    decoy.write_bytes(b"png-bytes")

    real_resolve = Path.resolve

    def resolve(inner: Path, *args: Any, **kwargs: Any) -> Path:
        return real_resolve(secret) if inner == decoy else real_resolve(inner)

    monkeypatch.setattr(Path, "resolve", resolve)

    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="private credential"):
            await client.call_tool(
                "expose_local_image", {"path": str(decoy)}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_no_tunnel_is_a_failure_not_a_link(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")
    dead = FakeTunnel(active=False, can_start=False)

    async with Client(server_for(dead)) as client:
        with pytest.raises(ToolError, match="not up"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(picture)},
                meta=client_meta("jack"),
            )


@pytest.mark.asyncio
async def test_an_unreachable_tunnel_refuses_instead_of_handing_out_a_link(
    tmp_path: Path,
) -> None:
    """A published URL is not a working one.  A transport that lost the edge
    keeps its URL and answers every request to it with HTTP 530, so handing it
    out is handing out a dead link — and the caller finds out later, from a
    model that could not fetch the file."""
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")
    flapping = FakeTunnel(reachable=False)

    async with Client(server_for(flapping)) as client:
        with pytest.raises(ToolError, match="530"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(picture)},
                meta=client_meta("jack"),
            )


@pytest.mark.asyncio
async def test_a_tunnel_that_died_mid_call_is_a_failure(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")

    async with Client(server_for(FakeTunnel(vanishes=True))) as client:
        with pytest.raises(ToolError, match="went away"):
            await client.call_tool(
                "expose_local_image",
                {"path": str(picture)},
                meta=client_meta("jack"),
            )


# --- withdrawing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexposing_stops_the_link(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")
    server = server_for()
    app = server.http_app()

    async with Client(server) as client:
        token = exposed_url(
            await ask(client, "expose_local_image", path=str(picture))
        ).rsplit("/", 1)[-1]
        assert (await fetch(app, f"/share/{token}")).status_code == 200

        await ask(client, "unexpose_local_image", token=f"https://x/share/{token}")

    assert (await fetch(app, f"/share/{token}")).status_code == 403


@pytest.mark.asyncio
async def test_unexposing_something_unknown_is_refused() -> None:
    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="no link with the token"):
            await client.call_tool(
                "unexpose_local_image", {"token": "deadbeef"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_an_empty_token_is_refused() -> None:
    async with Client(server_for()) as client:
        with pytest.raises(ToolError, match="no token was given"):
            await client.call_tool(
                "unexpose_local_image", {"token": "  "}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_one_conversation_cannot_revoke_anothers_link(tmp_path: Path) -> None:
    """The tokens live in one map, because `GET /share/<token>` carries no
    identity — so without this rule any agent could kill any other's link."""
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")
    server = server_for()

    async with Client(server) as client:
        token = exposed_url(
            await ask(client, "expose_local_image", path=str(picture))
        ).rsplit("/", 1)[-1]
        result = await client.call_tool(
            "unexpose_local_image",
            {"token": token},
            meta=client_meta("someone-else"),
            raise_on_error=False,
        )
    assert "another conversation" in "".join(
        getattr(block, "text", "") for block in result.content or []
    )


# --- the tunnel's own report --------------------------------------------------


@pytest.mark.asyncio
async def test_status_says_what_the_tunnel_is_doing() -> None:
    async with Client(server_for()) as client:
        reported = await ask(client, "tunnel_status")

    assert '"state": "active"' in reported
    assert '"reachable": true' in reported
    assert '"provider": "ngrok"' in reported


@pytest.mark.asyncio
async def test_status_names_the_program_that_actually_ran() -> None:
    """A CLI provider resolves its binary on PATH first, then in guessed install
    locations (`providers._which`), so the provider name alone does not say which
    program is running.  `tunnel_status` reads the resolved `binary_path` — the
    same way it reads `edge_ip` — and reports it, so the path is readable from
    the one tool that reports the tunnel's live facts.
    """
    tunnel = FakeTunnel()
    tunnel.binary_path = "/opt/cf/cloudflared"

    async with Client(server_for(tunnel)) as client:
        reported = await ask(client, "tunnel_status")

    assert '"binary": "/opt/cf/cloudflared"' in reported


@pytest.mark.asyncio
async def test_reachable_is_reported_apart_from_active() -> None:
    """'A URL exists' and 'that URL would be served' are different facts, and a
    reader that conflates them reads an outage as healthy."""
    async with Client(server_for(FakeTunnel(reachable=False))) as client:
        reported = await ask(client, "tunnel_status")

    assert '"state": "active"' in reported
    assert '"reachable": false' in reported


@pytest.mark.asyncio
async def test_a_failed_tunnel_reports_its_reason() -> None:
    failed = FakeTunnel(active=False, can_start=False, state="failed", reason="boom")
    async with Client(server_for(failed)) as client:
        reported = await ask(client, "tunnel_status")

    assert '"state": "failed"' in reported
    assert '"reason": "boom"' in reported
    assert '"reachable": false' in reported


@pytest.mark.asyncio
async def test_status_counts_what_is_exposed(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"png-bytes")
    async with Client(server_for()) as client:
        assert '"exposed": 0' in await ask(client, "tunnel_status")
        await ask(client, "expose_local_image", path=str(picture))
        assert '"exposed": 1' in await ask(client, "tunnel_status")


# --- the start the first call depends on ---------------------------------------


@pytest.mark.asyncio
async def test_a_call_during_an_in_flight_start_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_ensure` exists for the seconds between the plugin being ready and the
    tunnel being established — which is exactly an in-flight eager start.

    Returning early there (its old shape) made the function contradict its own
    docstring: the first call was told "the tunnel is not up" for an attempt
    that was two seconds from answering.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "tunnel_retry_delay", 0.01)
    tunnel = SlowStartTunnel()

    async def finish_soon() -> None:
        await asyncio.sleep(0.05)
        tunnel.finish()

    finisher = asyncio.create_task(finish_soon())
    await _ensure(tunnel, 8038)

    # The wait is the fix: with the old early return, `_ensure` comes back
    # before the finisher runs and this is still False.
    assert tunnel.is_active is True
    # The in-flight attempt was the answer: no second start was attempted on
    # top of it (the provider would have refused one as "already in progress").
    assert tunnel.started == []
    await finisher


@pytest.mark.asyncio
async def test_a_start_that_outlives_its_budget_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that never concludes must end as an honest "not up" rather than
    a call that never returns, and must not race a second attempt at the
    provider — which the single-flight guard would refuse anyway."""
    monkeypatch.setattr(_T.timeouts.pacing, "tunnel_retry_delay", 0.01)
    monkeypatch.setattr(_T.timeouts.ready, "tunnel_start", 0.05)
    tunnel = SlowStartTunnel()  # never finishes

    await _ensure(tunnel, 8038)  # must return, not hang

    assert tunnel.is_active is False
    assert tunnel.started == []


# --- the route, through the real app ------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_token_is_403() -> None:
    response = await fetch(server_for().http_app(), "/share/deadbeef")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_route_serves_what_the_tool_exposed(tmp_path: Path) -> None:
    """The whole feature in one test: a tool call, a token, and the bytes a
    third party gets by fetching the URL it was handed."""
    payload = b"file-content-bytes"
    picture = tmp_path / "data.bin"
    picture.write_bytes(payload)
    server = server_for()
    app = server.http_app()

    async with Client(server) as client:
        token = exposed_url(
            await ask(client, "expose_local_image", path=str(picture))
        ).rsplit("/", 1)[-1]

    response = await fetch(app, f"/share/{token}")
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert response.headers["Content-Length"] == str(len(payload))
    assert response.headers["Content-Disposition"] == 'inline; filename="data.bin"'


@pytest.mark.asyncio
async def test_a_deleted_file_is_404(tmp_path: Path) -> None:
    picture = tmp_path / "gone.png"
    picture.write_bytes(b"x")
    exposure = Exposure("t" * 30, str(picture), None)
    picture.unlink()
    assert (await serve_exposed(exposure)).status_code == 404


@pytest.mark.asyncio
async def test_a_replaced_file_is_refused(tmp_path: Path) -> None:
    """The pin: a path swapped after the link was made must not serve the new
    content to everyone holding the old one."""
    picture = tmp_path / "data.txt"
    picture.write_bytes(b"original")
    exposure = Exposure("t" * 30, str(picture), None)
    picture.write_bytes(b"replaced, and longer than before")
    assert (await serve_exposed(exposure)).status_code == 403


@pytest.mark.asyncio
async def test_a_non_latin_filename_does_not_break_the_header(tmp_path: Path) -> None:
    """HTTP headers are Latin-1.  A CJK filename used to raise
    `UnicodeEncodeError` inside Starlette while writing the header — a 500 for
    a file whose only sin was its name."""
    payload = b"\xe4\xb8\xad\xe6\x96\x87"
    picture = tmp_path / "报告.pdf"
    picture.write_bytes(payload)
    exposure = Exposure("t" * 30, str(picture), None)

    response = await serve_exposed(exposure)
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E6%8A%A5%E5%91%8A.pdf" in disposition
    assert b"".join([chunk async for chunk in response.body_iterator]) == payload


# --- the guards, as units -----------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("photo.png", 'inline; filename="photo.png"'),
        ('od"d.png', 'inline; filename="od_d.png"'),
    ],
)
def test_content_disposition_for_a_latin1_name(name: str, expected: str) -> None:
    assert content_disposition(name) == expected


def test_content_disposition_carries_a_non_latin_name_twice() -> None:
    disposition = content_disposition("报告.pdf")
    assert 'filename="' in disposition  # an ASCII fallback for old clients
    assert "filename*=UTF-8''" in disposition  # the real name, per RFC 5987


@pytest.mark.parametrize(
    "path, refused",
    [
        (".env", True),
        (".env.local", True),
        ("keys/id_rsa", True),
        ("id_ed25519", True),
        ("sub/dir/.npmrc", True),
        ("photo.png", False),
        ("environment.py", False),  # not a `.env` just because it starts so
        ("report.pdf", False),
    ],
)
def test_which_paths_look_like_credentials(path: str, refused: bool) -> None:
    assert is_credential_path(Path(path)) is refused


# --- the registry -------------------------------------------------------------


def test_a_token_is_30_hex_characters() -> None:
    """Deliberately under 32: that is the length a generic redactor treats as a
    secret, and a share link is one that has to survive being written down."""
    exposure = Exposed().register("/some/file.txt", None)
    assert len(exposure.token) == 30
    assert all(character in "0123456789abcdef" for character in exposure.token)


def test_a_token_is_the_whole_capability(tmp_path: Path) -> None:
    """A call that names no conversation still gets a link — the route that
    serves it never sees who is asking, so the token is what protects it."""
    picture = tmp_path / "a.png"
    picture.write_bytes(b"x")
    exposed = Exposed()
    exposure = exposed.register(str(picture), None)
    assert exposed.lookup(exposure.token) is exposure
    # ...and an unowned link is anyone's to withdraw.
    assert exposed.revoke(exposure.token, ("jack", "")) is Revoked.OK
    assert exposed.lookup(exposure.token) is None


def test_distinct_files_get_distinct_tokens(tmp_path: Path) -> None:
    one, two = tmp_path / "a.png", tmp_path / "b.png"
    one.write_bytes(b"a")
    two.write_bytes(b"b")
    exposed = Exposed()
    assert (
        exposed.register(str(one), None).token != exposed.register(str(two), None).token
    )


def test_an_unknown_token_looks_up_to_nothing() -> None:
    assert Exposed().lookup("deadbeef") is None


def test_a_file_deleted_between_the_check_and_the_register(tmp_path: Path) -> None:
    """Every check-then-act has that window, and it has to close as a sentence
    rather than a traceback: `expose_local_image` proves the file exists and
    then registers it, and the file may be gone by the second step."""
    picture = tmp_path / "gone.png"
    picture.write_bytes(b"x")
    exposed = Exposed()
    exposed.register(str(picture), None)
    picture.unlink()

    again = exposed.register(str(picture), None)  # must not raise
    assert again.token
    assert Exposure("t" * 30, str(picture), None).matches(None) is False


@pytest.mark.asyncio
async def test_a_link_minted_while_the_file_was_absent_never_serves(
    tmp_path: Path,
) -> None:
    """The pin is what makes a link safe, so a link with no pin must refuse.

    `Exposure.__init__` swallows the `OSError` when the file is missing for the
    moment `register` looks, and `expose_local_image` checked a moment before —
    so registering anyway is the honest reading.  What that used to leave was an
    exposure whose `matches()` answered True to *anything* ("nothing was
    pinned, so nothing can have changed"), so bytes written at the path
    afterwards were served under a token already handed out.  With no baseline
    there is nothing to compare, so the unpinned link is refused instead.
    """
    ghost = tmp_path / "ghost.bin"
    exposure = Exposed().register(str(ghost), None)  # absent → nothing to pin
    assert exposure.ino is None

    ghost.write_bytes(b"bytes that appeared after the link was made")
    assert (await serve_exposed(exposure)).status_code == 403


def test_the_config_the_shipped_file_names_is_one_this_build_builds() -> None:
    """`slife2.yaml` is what every install seeds, so a provider it ships that
    this build cannot construct is a tunnel that fails on first run.

    Reads the real file, which means resolving its `${VAR}` references and so
    touching credstore — the same thing v1's equivalent test did, and the point
    of it: a shipped config that only parses on the machine it was written on
    is the bug this catches.
    """
    root = REPO
    section = load(root / "slife2.yaml").file_tunnel
    assert section.active in KNOWN_PROVIDERS
    for name in KNOWN_PROVIDERS:
        assert name in section.providers, f"{name} missing from slife2.yaml"


def test_the_default_active_provider_is_the_module_constant() -> None:
    """Spelled out in `slife2/libs/host/config.py` rather than imported, by the rule that
    keeps the TUI and the agent loop from pulling in a tunnel's SDK — so the two
    ends of that fact are pinned here."""
    assert FileTunnelSettings().active == DEFAULT_PROVIDER


def test_the_shipped_config_reaches_the_plugin() -> None:
    """The section, the `servers:` entry and the plugin's own key have to agree
    or the plugin starts with no address to forward to."""
    root = REPO
    config = load(root / "slife2.yaml")
    assert config.server("file-tunnel").port == 8038
    assert "file-tunnel" in config.plugins()
