"""The iLink ClawBot protocol: WeChat, as much of it as a program may have.

WeChat opens no API to a program.  What exists is a hosted bridge — iLink's
ClawBot endpoints on `ilinkai.weixin.qq.com` — and this module is the whole of
what slife2 knows about it: the headers a request carries, the six endpoints
behind them, the QR handshake that turns a phone into a token, the long poll
that token buys, and the envelopes that mean something other than what their
status code says.

**A library, and not a plugin**, by the test DESIGN.md §1.1 states: what holds a
credential is a plugin, and this holds none — a token is handed to it by
`slife2-wechat`, which is the process that owns the session file.  The split is
v1's (`slife/plugins/wechat/client.py`) and it is kept for the reason that split
always earns: the protocol can be driven end to end with no server anywhere,
because :class:`Http` is the seam and the real implementation is four lines.

Adapted from SiverKing/weixin-ClawBot-API (MIT License).

## Two envelopes a status code does not describe

iLink answers a **revoked or rejected token** with `200 OK` and a body carrying
`errno` / `error` / `retcode` instead of `msgs`.  A client that reads `msgs` and
defaults to "no messages" spins forever against a dead token while every status
question answers `logged_in` — which is what v1 did until the envelope was read,
and why :func:`error_envelope` exists as a thing named rather than a check
inlined at each of the three places it is needed.

**And the same "no" comes in a second spelling**, which is how this defect came
back after being fixed once.  iLink keeps **one live session per WeChat
account**: scanning a code for a second agent — or on a second machine — with the
same phone retires the older login, and the retired one is then answered
`{"errcode": -14, "errmsg": "session timeout"}` to its polls *and its sends*,
instantly and for as long as the token is offered.  Nothing about that body is
new: it is a rejection wearing a `200`, the thing this section is about.  What
was new is the pair of keys — a reader that knew `errno` and not `errcode` read
it as an empty poll, so the retired session kept a poll loop, kept answering
`logged_in`, and kept **reporting a send as delivered that the bridge had already
thrown away**.  Both pairs are read (see :data:`ENVELOPE_ERROR_KEYS`), and the
window is told its link is down by the same loop that used to spin.

The **`get_updates_buf`** cursor is the other half of the same problem.  iLink
re-serves recent messages until the cursor advances, so a client that does not
persist it re-ingests the whole replay window after every restart.  The cursor
is this client's state and the *file* is the plugin's business; `updates_buf`
is where the two meet.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx2

import slife2.timeouts as _T

logger = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"

#: The client the far end is told it is talking to.  `2.4.3` is v1's value and
#: is not this project's version: it is the *bridge's* protocol revision, and
#: the far end is entitled to answer differently to a different one.
CHANNEL_VERSION = "2.4.3"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 3)
BOT_AGENT = "slife2-wechat/1.0.0 (python)"

#: How long one call may take.  Generous, because the poll is a *long* poll —
#: the far end holds the request open until it has something to say — and v1's
#: number is kept: it is the far end's cadence, not ours to shorten.


@dataclass(frozen=True)
class Answer:
    """What a call came back with: the status, and the body as it arrived.

    The body stays text because iLink's failures are *envelopes*, not statuses:
    a rejected token is a `200` carrying `{"errno": …}`, so this module has to
    read the body before it can say whether the call worked.  Decoding is
    therefore the caller's — the one place that knows whether a body is
    expected to be JSON.
    """

    status: int
    text: str


class Http(Protocol):
    """One HTTP round trip, as this protocol needs it.

    **The seam is the request, not the socket.**  What a test wants to replace
    is the protocol: a fake answers these six endpoints with the envelopes a
    real bot sees — a scan that redirects to another node, a token that gets
    revoked mid-session — and no socket is involved at all.
    """

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout: float,
    ) -> Answer: ...


async def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any] | None,
    timeout: float,
) -> Answer:
    """The real :class:`Http`: one `httpx2` request, and its body as text.

    **A client per call, and deliberately.**  One pooled client would be fewer
    handshakes, and it is what v1 could not do: an `aiohttp` session binds to
    the event loop that made it, and v1's own notes are a record of sessions
    outliving their loops across a restart.  A context manager per call cannot
    be bound to a loop that is gone, and the cost is one TCP handshake per poll
    — against a poll that is a long-poll by design.

    Nothing here logs the body.  `get_qrcode_status` answers with the **live
    bot token** in it, so a debug line with the body in it is a credential in a
    plaintext file — v1 masked the text with a helper it had to remember to
    call at both sites; this does not have the text to leak.
    """
    kwargs: dict[str, Any] = {} if body is None else {"json": dict(body)}
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(timeout)) as session:
        response = await session.request(method, url, headers=dict(headers), **kwargs)
        logger.debug("ilink %s %s -> %s", method, url, response.status_code)
        return Answer(status=response.status_code, text=response.text)


def headers(token: str | None = None) -> dict[str, str]:
    """The headers every iLink call carries.

    `X-WECHAT-UIN` is a random number, base64'd, regenerated per call — it is
    what the bridge counts a *device* by, and a constant one is a device that
    never reconnects.  v1's implementation and v1's comment; the randomness is
    load-bearing rather than decorative.
    """
    uin = str(random.randint(0, 0xFFFFFFFF))
    made = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        made["Authorization"] = f"Bearer {token}"
    return made


def base_info() -> dict[str, str]:
    """The two fields every body carries, saying which client is asking."""
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


#: The keys the bridge says "no" with.  Two spellings of one thing, and the
#: second is not decoration: a session a newer login of the same WeChat account
#: has taken over is answered `{"errcode": -14, "errmsg": "session timeout"}`,
#: which a reader that knew only the first pair read as a healthy empty poll —
#: see the module docstring.  **`errmsg` is deliberately not here**: it is the
#: *text* of a "no", and a body carrying text without a verdict has not said one.
ENVELOPE_ERROR_KEYS = ("errno", "error", "retcode", "errcode")

#: Where that "no"'s own words are read off, most specific first — `errmsg`
#: before the numeric codes because a `-14` is a number nobody can act on.
ENVELOPE_TEXT_KEYS = ("error", "errno", "retcode", "errmsg", "errcode")


def error_envelope(result: Mapping[str, Any]) -> bool:
    """Whether an iLink body is a *failure* wearing a `200`.

    See the module docstring: any of :data:`ENVELOPE_ERROR_KEYS` is the bridge
    saying "no" in a successful response, and a client that only reads `msgs`
    reads every one of them as silence.
    """
    return any(result.get(key) for key in ENVELOPE_ERROR_KEYS)


def envelope_error(result: Mapping[str, Any]) -> str:
    """What the bridge said, in its own words — `""` when it said nothing.

    **The failure and nothing around it**, because every caller is already
    writing that sentence: a caller that handed this to a model prefixed with
    "the server rejected the login" would otherwise have it say "the server
    rejected the login (session rejected by server: session timeout)", and the
    half worth reading is the bridge's (`session timeout`), not ours twice.

    `errmsg` is preferred to the numeric codes for the same reason: a `-14` is a
    number nobody can act on.  An envelope that carries only numbers answers
    with one, which is the bridge being terse rather than this being empty.
    """
    for key in ENVELOPE_TEXT_KEYS:
        if result.get(key):
            return str(result[key])
    return ""


class IlinkClient:
    """One bot's session: the token, the cursor, and the calls made over them.

    The QR handshake is driven by `slife2-wechat` rather than by this class —
    the login is a *turn*'s worth of waiting and a status a person watches, and
    what belongs here is the four calls it is made of (`fetch_qrcode`,
    `poll_login_status`, `start`, `get_session_dict`).
    """

    def __init__(self, *, http: Http = request) -> None:
        self._http = http
        self._bot_token: str = ""
        self._base_url: str = BASE_URL
        self._get_updates_buf: str = ""
        self._typing_tickets: dict[str, str] = {}
        #: The bot's own iLink identity, from the login.  Not the peer's: the
        #: `getconfig` endpoint that mints a typing ticket is a *bot*-level
        #: call, which is the distinction v1 lost once and wrote down.
        self._ilink_user_id: str = ""
        self._ilink_bot_id: str = ""
        #: Who last messaged, so the model can be told who a reply goes to.
        self.last_contact: dict[str, str | None] = {
            "from_id": None,
            "context_token": None,
        }
        #: Session health, and the two are different questions.  `last_error`
        #: is the most recent failure of any kind, `auth_failed` is the one
        #: kind that needs a fresh login rather than a retry.
        self.last_error: str | None = None
        self.auth_failed: bool = False

    # ── The QR handshake ─────────────────────────────────────────────────

    async def fetch_qrcode(self, base_url: str = "") -> dict[str, Any]:
        """A fresh login QR, as the bridge describes it.

        POST first and GET as a fallback: the documented call is the POST, and
        the GET is what v1 found answering when the POST came back without a
        `qrcode` — kept because a login that cannot start is a login that
        cannot be retried.
        """
        data = await self._post(
            "ilink/bot/get_bot_qrcode?bot_type=3",
            {"local_token_list": []},
            base_url,
        )
        if data.get("qrcode"):
            return data
        logger.debug("qr_post_empty action=get_fallback")
        return await self._get("ilink/bot/get_bot_qrcode?bot_type=3", base_url)

    async def poll_login_status(
        self, qrcode: str, base_url: str, verify_code: str | None = None
    ) -> dict[str, Any]:
        """Where one QR is in its life, as one of a small set of facts.

        **The bridge's states are translated here rather than at the caller**,
        because they are its vocabulary and the caller's is a login: `scaned`,
        `confirmed`, `expired`, `binded_redirect`, and the verify-code pair are
        six words for what is a four-state loop, and the loop that acts on them
        is the plugin's.

        `redirect_base` is the one that matters and the one v1 shipped without:
        a code scanned on the wrong node answers `scaned_but_redirect`, and a
        loop that keeps polling the original host waits forever for a
        confirmation that went somewhere else.
        """
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        status = await self._get(endpoint, base_url)
        state = str(status.get("status", ""))

        if state == "confirmed" or status.get("bot_token"):
            return {
                "bot_token": status.get("bot_token"),
                "baseurl": status.get("baseurl") or status.get("base_url") or base_url,
                "ilink_bot_id": status.get("ilink_bot_id"),
                "ilink_user_id": status.get("ilink_user_id"),
            }
        if state == "binded_redirect" or status.get("binded_redirect"):
            return {"already_connected": True}
        if state == "expired":
            return {"expired": True}
        if state == "scaned_but_redirect":
            redirect_host = status.get("redirect_host")
            return (
                {"redirect_base": f"https://{redirect_host}"} if redirect_host else {}
            )
        if state == "scaned":
            return {"scanned": True, "verify_code_accepted": bool(verify_code)}
        if state in ("need_verifycode", "verify_code_blocked") or status.get(
            "need_verifycode"
        ):
            if state == "verify_code_blocked":
                return {"verify_code_blocked": True}
            return {"need_verifycode": True, "retry_verifycode": bool(verify_code)}
        if state and state != "wait":
            logger.debug("login_poll status=%s", state)
        return {}

    # ── The session ──────────────────────────────────────────────────────

    async def start(
        self,
        bot_token: str,
        base_url: str = "",
        ilink_user_id: str = "",
        ilink_bot_id: str = "",
    ) -> None:
        """Adopt a token, and the identity that came with it."""
        self._bot_token = bot_token
        self._base_url = base_url or BASE_URL
        self._ilink_user_id = ilink_user_id
        self._ilink_bot_id = ilink_bot_id

    async def try_restore_session(
        self, saved: Mapping[str, Any] | None, *, max_age: float
    ) -> bool:
        """Adopt a saved session, if it is neither empty nor too old.

        **Age only** — a token the far end has revoked passes this, which is
        why :meth:`validate_session` exists and why the caller runs them in
        that order.  `max_age` is the caller's, because the lifetime of a
        session is a policy about *this login* and not a fact about the
        protocol; v1 kept it in a global timeouts registry, which is how a
        number about WeChat came to be read out of a module about everything.
        """
        if not saved:
            return False
        if time.time() - float(saved.get("saved_at") or 0) > max_age:
            logger.debug("session_expired max_age=%s", max_age)
            return False
        bot_token = saved.get("bot_token")
        if not bot_token:
            return False
        await self.start(
            str(bot_token),
            str(saved.get("base_url") or BASE_URL),
            ilink_user_id=str(saved.get("ilink_user_id") or ""),
            ilink_bot_id=str(saved.get("ilink_bot_id") or ""),
        )
        return True

    async def validate_session(self) -> bool:
        """Whether the far end still accepts this token.

        Returns `False` for *any* failure and records why in `last_error`; the
        caller tells the two apart by `auth_failed`, which is set only when the
        far end said no.  A network failure is transient and a revoked token is
        not, and treating them as one thing is how a session gets thrown away
        over a flaky moment.

        An envelope that is neither an error nor a message list is read as
        *valid*: this can only revoke on evidence, and an ambiguous answer is
        not evidence.
        """
        result = await self._getupdates()
        return result is not None and not error_envelope(result)

    async def _getupdates(self) -> dict[str, Any] | None:
        """One `getupdates` call, with the failure it can have already recorded.

        **Both readers of this endpoint are one call**, and this is where the two
        cannot drift apart: :meth:`validate_session` and :meth:`poll_updates` ask
        the same question, advance the same cursor and read the same envelope —
        an error wearing a `200` — and the only difference between them is what
        they want back.  A copy of the envelope handling in each is a copy that
        comes to disagree about what "rejected" is, which is the failure this
        module's docstring was written about.

        `None` means the call did not produce a body to read — a transport error
        or a non-object envelope — and `last_error` says why.  A returned mapping
        has had its envelope decided here: if it is an error, `auth_failed` and
        `last_error` are set; otherwise `last_error` is cleared.  So a caller only
        has to decide the answer that is *its own*, which is the one thing the
        two readers do differently.
        """
        try:
            result = await self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self._get_updates_buf, "base_info": base_info()},
            )
        except Exception as exc:  # noqa: BLE001 — recorded, and the loop retries
            self.last_error = str(exc)
            return None
        if not isinstance(result, dict):
            self.last_error = f"unexpected getupdates envelope: {result!r}"
            return None
        if error_envelope(result):
            self.auth_failed = True
            self.last_error = envelope_error(result) or "session rejected"
            return result
        self.last_error = None
        return result

    def get_session_dict(self) -> dict[str, Any]:
        """This session, as the plugin writes it down.

        `saved_at` is stamped here rather than by the writer, so that what is
        saved is when the *login* happened — the age the next start measures.
        """
        return {
            "bot_token": self._bot_token,
            "base_url": self._base_url,
            "ilink_user_id": self._ilink_user_id,
            "ilink_bot_id": self._ilink_bot_id,
            "saved_at": time.time(),
        }

    @property
    def is_logged_in(self) -> bool:
        """Whether there is a token at all.  Not whether it still works."""
        return bool(self._bot_token)

    @property
    def updates_buf(self) -> str:
        """The ack cursor: handing it back tells the bridge what has been seen."""
        return self._get_updates_buf

    @updates_buf.setter
    def updates_buf(self, value: str) -> None:
        self._get_updates_buf = value or ""

    def reject_session(self) -> None:
        """Drop the credentials, after the far end refused them.

        `try_restore_session` adopts a token before anything confirms it, so a
        token that turns out to be dead has to be *removed* — otherwise
        `is_logged_in` keeps answering yes and every status read is a phantom.
        The session file is not touched: forgetting it is the caller's decision
        and a different one.
        """
        self._bot_token = ""
        self._base_url = BASE_URL
        self._get_updates_buf = ""
        self._ilink_user_id = ""
        self._ilink_bot_id = ""
        self.last_contact = {"from_id": None, "context_token": None}
        self.last_error = None
        self.auth_failed = False

    def clear_session_faults(self) -> None:
        """Forget the faults, after something proved the link works again."""
        self.last_error = None
        self.auth_failed = False

    # ── Messages ─────────────────────────────────────────────────────────

    async def poll_updates(self) -> list[dict[str, Any]]:
        """Long-poll for messages, advancing the ack cursor.

        **A failed poll returns `[]` rather than raising**, and records why in
        `last_error`.  That is not swallowing an error: the caller's loop is a
        *poll*, and every outcome it can have is a reason to poll again a
        moment later — what it needs from a failure is the fact, which is what
        `last_error` is, so that the status tool can tell the model the link is
        down instead of reporting a healthy session that has been quietly
        collecting nothing for an hour.

        The call itself and the envelope it can wear are :meth:`_getupdates`'s,
        shared with :meth:`validate_session` so the two cannot disagree about
        what a rejection is; what is left here is the cursor and the messages,
        which only this reader wants.
        """
        result = await self._getupdates()
        if result is None or error_envelope(result):
            return []
        self._get_updates_buf = result.get("get_updates_buf") or self._get_updates_buf
        msgs = result.get("msgs")
        return (
            [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
        )

    async def send_message(
        self, to_user_id: str, context_token: str, text: str
    ) -> dict[str, Any]:
        """Send one text message.

        The body is the bridge's shape, and every field in it is load-bearing:
        `message_type: 2` and `message_state: 2` are what make it a *sent* text
        rather than a draft, and `client_id` is what the far end deduplicates a
        retry by — so it is random per call rather than derived from the text,
        which would make two identical messages one message.
        """
        client_id = f"slife2-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
        return await self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": base_info(),
            },
        )

    async def send_typing(
        self, to_user_id: str, context_token: str, status: int = 1
    ) -> dict[str, Any] | None:
        """Show (`status=1`) or hide (`status=2`) the typing indicator.

        Silent when no ticket can be had: a typing indicator is a courtesy and
        a failed one is not a failed turn.
        """
        ticket = await self._typing_ticket(to_user_id, context_token)
        if not ticket:
            return None
        return await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": to_user_id,
                "typing_ticket": ticket,
                "status": status,
                "base_info": base_info(),
            },
        )

    async def _typing_ticket(self, user_id: str, context_token: str) -> str:
        """The per-conversation ticket a typing indicator needs, cached.

        `getconfig` is a **bot-level** endpoint: it wants the bot's own iLink
        id, which the login returned, and not the id of whoever is being
        answered — that one goes to `sendtyping` as the target.  Passing the
        sender's id here is the mistake v1 made and wrote down.
        """
        cached = self._typing_tickets.get(user_id)
        if cached:
            return cached
        bot_id = self._ilink_user_id or self._ilink_bot_id
        body: dict[str, Any] = {"base_info": base_info()}
        if bot_id:
            body["ilink_user_id"] = bot_id
        if context_token:
            body["context_token"] = context_token
        config = await self._post("ilink/bot/getconfig", body)
        ticket = str(config.get("typing_ticket") or "")
        if ticket:
            self._typing_tickets[user_id] = ticket
        else:
            logger.debug("typing_ticket_empty ilink_user_id=%s", bot_id)
        return ticket

    # ── The wire ─────────────────────────────────────────────────────────

    async def _get(self, path: str, base_url: str = "") -> dict[str, Any]:
        return await self._call("GET", path, None, base_url)

    async def _post(
        self, path: str, body: Mapping[str, Any], base_url: str = ""
    ) -> dict[str, Any]:
        return await self._call("POST", path, body, base_url)

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        base_url: str,
    ) -> dict[str, Any]:
        """One call: build the URL, hand it to the seam, decode what comes back.

        A body that is not JSON decodes to `{}` rather than raising, which is
        the same answer as an empty one and is what the callers already handle:
        the two ways to fail are "the bridge said no" (an envelope, read by the
        caller) and "there is nothing here to read", and neither is a reason to
        take down a poll loop.
        """
        url = f"{base_url or self._base_url}/{path}"
        answer = await self._http(
            method,
            url,
            headers=headers(self._bot_token),
            body=body,
            timeout=_T.timeouts.transport.wechat_request,
        )
        try:
            decoded = json.loads(answer.text)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
