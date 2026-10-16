"""`slife2-media`: the four tools, the config's four steps, and two adapters.

Three halves, and each is tested where it can be driven without a network.  The
*tools* run over the in-memory transport with a fake adapter injected, so what is
under test is which provider, which model, which parameters and which folder a
call reaches.  The *config* is parsed from text.  The *adapters* are driven
against a fake `_request`, which is where the vendor's shapes live — the request
body, the poll, and the four ways a call can fail.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import (
    ConfigError,
    MediaProviderSettings,
    MediaSettings,
    _media,
    load,
)
from slife2.libs.host.paths import agent_dir
from slife2.libs.plugin.audience import client_meta, for_the_model
from slife2.plugins.media import adapters
from slife2.plugins.media.adapters import (
    DashScopeAIGCAdapter,
    MediaAdapterError,
    OpenAICompatAdapter,
    merge_params,
)
from slife2.plugins.media.server import build_server, check_size

pytestmark = pytest.mark.unit

#: A config with one provider of all four kinds, written the way a file is.
SECTION = {
    "defaults": {"image": "test/img", "video": "test/vid"},
    "providers": {
        "test": {
            "api": "dashscope-aigc",
            "base_url": "https://example.test/api/v1/",
            "api_key": "k",
            "models": [
                {"model": "img", "kind": "image"},
                {"model": "img-pro", "kind": "image", "params": {"quality": "high"}},
                {
                    "model": "vid",
                    "kind": "video",
                    "params": {"resolution": "720P", "duration": 5},
                },
                {"model": "say", "kind": "tts", "voice": "v1"},
                {"model": "hear", "kind": "asr"},
            ],
        }
    },
}


def settings(raw: Any = None) -> MediaSettings:
    return _media(SECTION if raw is None else raw, MediaSettings())


class FakeAdapter:
    """An adapter that records its calls and answers with a path.

    The tool half is about *addressing* — which provider, model, parameters and
    folder a call reaches — and a fake that records exactly that is what makes
    those assertions readable.  v1's tests do the same with a `MagicMock`; a
    class with four methods says what the contract is.
    """

    def __init__(self, *, answer: str = "/tmp/out") -> None:
        self.answer = answer
        self.calls: dict[str, dict[str, Any]] = {}

    async def generate_image(self, **kwargs: Any) -> str:
        self.calls["image"] = kwargs
        return self.answer

    async def generate_video(self, **kwargs: Any) -> str:
        self.calls["video"] = kwargs
        return self.answer

    async def text_to_speech(self, **kwargs: Any) -> str:
        self.calls["tts"] = kwargs
        return self.answer

    async def transcribe_audio(self, **kwargs: Any) -> str:
        self.calls["asr"] = kwargs
        return "hello home"

    async def close(self) -> None:
        self.calls["closed"] = {}


class Failing(FakeAdapter):
    """An adapter whose every capability raises what it was given."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def generate_image(self, **kwargs: Any) -> str:
        raise self.error


def server_for(adapter: FakeAdapter | None = None) -> FastMCP:
    """The plugin over a config built in code, with one fake adapter."""
    config = replace(load(), media=settings())
    return build_server(config, adapters={"test": adapter or FakeAdapter()})


async def ask(client: Client, tool: str, **arguments: Any) -> Any:
    """One tool call, on behalf of a conversation: the tool's own answer.

    `result.data` is the object FastMCP parsed out of the tool's JSON, which is
    the answer as the model reads it — so an assertion here is about a fact
    rather than about the spelling of one.  A refusal raises (`ToolError`),
    which `refused` is for.
    """
    result = await client.call_tool(tool, arguments, meta=client_meta("jack"))
    return result.data


async def refused(client: Client, tool: str, **arguments: Any) -> str:
    """A refusal, as the model reads it: the sentence in the error result.

    **A second reader on purpose.**  `ask` is the success path and raises for a
    call that failed — a test that read a refusal out of it could not tell one
    from an answer — so the tests that are *about* a refusal come through here.
    """
    result = await client.call_tool(
        tool, arguments, meta=client_meta("jack"), raise_on_error=False
    )
    return "".join(getattr(block, "text", "") for block in result.content or [])


# --- the surface --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_four_tools_are_the_models() -> None:
    """Four capabilities, all marked, and named as v1 named them."""
    async with Client(server_for()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {
        "generate_image",
        "generate_video",
        "text_to_speech",
        "transcribe_audio",
    }
    assert all(for_the_model(tool.meta) for tool in listed.values())


# --- which provider, model, parameters and folder -----------------------------


@pytest.mark.asyncio
async def test_an_image_call_reaches_the_configured_default() -> None:
    adapter = FakeAdapter(answer="/media/a.png")
    async with Client(server_for(adapter)) as client:
        answer = await ask(client, "generate_image", prompt="a cat", size="1024*1024")

    assert answer == {"path": "/media/a.png"}
    call = adapter.calls["image"]
    assert call["model"] == "img", "`defaults.image` names it"
    assert call["prompt"] == "a cat"
    assert call["size"] == "1024*1024"
    assert call["extra_params"] == {"size": "1024*1024"}
    assert call["outputs_dir"] == agent_dir("jack") / "media"


@pytest.mark.asyncio
async def test_a_models_own_parameters_sit_under_the_calls() -> None:
    """The config may pin a resolution; the call may override it.

    `merge_params`, and it is what lets a config say "this model always renders
    720P" without a tool growing a field per provider.
    """
    adapter = FakeAdapter()
    async with Client(server_for(adapter)) as client:
        await ask(client, "generate_video", prompt="bounce", duration=8)

    params = adapter.calls["video"]["extra_params"]
    assert params == {"resolution": "720P", "duration": 8}, "the call wins"
    assert adapter.calls["video"]["deadline_s"] is None, "the default deadline"


@pytest.mark.asyncio
async def test_a_voice_falls_back_to_the_config_and_a_model_can_be_named() -> None:
    adapter = FakeAdapter()
    async with Client(server_for(adapter)) as client:
        await ask(client, "text_to_speech", text="hi")
        assert adapter.calls["tts"]["voice"] == "v1"
        await ask(client, "text_to_speech", text="hi", voice="other", model="say")
        assert adapter.calls["tts"]["voice"] == "other"
        assert adapter.calls["tts"]["model"] == "say"


@pytest.mark.asyncio
async def test_a_folder_is_a_subdirectory_of_the_media_root() -> None:
    adapter = FakeAdapter()
    async with Client(server_for(adapter)) as client:
        await ask(client, "generate_image", prompt="x", folder="project")
        escaped = await refused(
            client, "generate_image", prompt="x", folder="../../elsewhere"
        )

    assert (
        adapter.calls["image"]["outputs_dir"] == agent_dir("jack") / "media" / "project"
    )
    assert "not a folder inside the media directory" in escaped, (
        "a model does not choose where to write"
    )


@pytest.mark.asyncio
async def test_a_transcript_is_the_answer_and_writes_nothing() -> None:
    adapter = FakeAdapter()
    async with Client(server_for(adapter)) as client:
        answer = await ask(client, "transcribe_audio", path="a.wav")

    assert answer["text"] == "hello home"
    assert answer["total"] == len("hello home")
    assert answer["offset"] == 0, "the whole of it was in one page"
    assert adapter.calls["asr"]["audio_path"] == Path("a.wav")


class Says(FakeAdapter):
    """An adapter whose transcript is whatever a test hands it."""

    def __init__(self, transcript: str) -> None:
        super().__init__()
        self.transcript = transcript

    async def transcribe_audio(self, **kwargs: Any) -> str:
        self.calls["asr"] = kwargs
        return self.transcript


@pytest.mark.asyncio
async def test_a_transcript_is_read_in_pages() -> None:
    """**The one answer here whose length is the recording's.**  An hour of
    speech is a text no single answer may carry, so a page is read by offset and
    `offset + len(text) < total` is how the next one is asked for.
    """
    async with Client(server_for(Says("abcdefghij"))) as client:
        first = await ask(client, "transcribe_audio", path="a.wav", limit=4)
        second = await ask(
            client,
            "transcribe_audio",
            path="a.wav",
            offset=first["offset"] + len(first["text"]),
            limit=4,
        )

    assert first["text"] == "abcd"
    assert first["total"] == 10, "the whole transcript, not the page"
    assert second["text"] == "efgh"
    assert second["offset"] == 4
    assert second["offset"] + len(second["text"]) < second["total"], "more remains"


@pytest.mark.asyncio
async def test_a_page_never_outgrows_the_tool_result_budget() -> None:
    """**`limit` counts characters and the budget counts tokens**, so the budget
    is the last word: a page of Chinese is several times what a page of English
    of the same length costs, and an answer over `tool_result.tokens` would be a
    result the harness had to shorten again — our own reader feeding our own
    shortening.
    """
    whole = "字" * 9000
    async with Client(server_for(Says(whole))) as client:
        page = await ask(client, "transcribe_audio", path="a.wav")

    assert page["total"] == 9000
    assert len(page["text"]) < 9000, "the budget cut the page short"
    assert "do not fit" in page["text"], "and the cut is announced"


# --- the refusals -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_size_is_checked_before_the_call_is_paid_for() -> None:
    """The one argument a model gets plausibly wrong, refused here rather than
    at the provider, where the refusal arrives after the call has been billed."""
    adapter = FakeAdapter()
    async with Client(server_for(adapter)) as client:
        bad = await refused(client, "generate_image", prompt="x", size="1024")
        small = await refused(client, "generate_image", prompt="x", size="10x10")

    assert "is not a size" in bad
    assert "too small" in small
    # `close` is recorded by the lifespan's teardown, so the question is which
    # *capabilities* were reached — and none were.
    assert not {"image", "video", "tts", "asr"} & set(adapter.calls)


@pytest.mark.asyncio
async def test_a_capability_the_adapter_lacks_names_the_adapter() -> None:
    """A `tts` model pointed at an images-only endpoint is a config to revisit,
    and the sentence has to say which half is missing."""
    failing = Failing(NotImplementedError("speech (this adapter draws images only)"))
    async with Client(server_for(failing)) as client:
        answer = await refused(client, "generate_image", prompt="x")

    assert "cannot make a image" in answer, "the noun names what was asked for"
    assert "Another `api:`" in answer, "and the answer says what would fix it"


@pytest.mark.asyncio
async def test_a_provider_failure_is_a_failure_not_a_transcript_entry() -> None:
    """v1 answered `"Error: …"` as text; here it is a `ToolError`, because a
    refusal the transcript reads as a success is what that convention costs."""
    failing = Failing(MediaAdapterError("the media API answered 401: nope", 401))
    async with Client(server_for(failing)) as client:
        # Not through `ask`: that reads the failure as text, deliberately, and
        # this test is about the failure being one.
        with pytest.raises(ToolError, match="401"):
            await client.call_tool(
                "generate_image", {"prompt": "x"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_no_media_section_is_a_sentence_not_a_crash() -> None:
    config = replace(load(), media=MediaSettings())
    async with Client(build_server(config)) as client:
        answer = await refused(client, "generate_image", prompt="x")

    assert "No image model configured" in answer
    assert "`media:`" in answer


@pytest.mark.asyncio
async def test_an_unknown_api_is_a_sentence_not_a_raw_error() -> None:
    """**A config with `api: typo` loads cleanly** — the loader only requires the
    key to be there — and then fails at the first call, where `create_adapter`
    raises.  Resolving the adapter *outside* `_run` let that failure escape as a
    raw exception instead of the sentence §1.5 promises, so the
    resolution is inside the guard and this checks the sentence is what arrives.
    """
    section = {
        "defaults": {"image": "typo/img"},
        "providers": {
            "typo": {
                "api": "not-an-adapter",
                "base_url": "https://example.test/api/v1/",
                "models": [{"model": "img", "kind": "image"}],
            }
        },
    }
    config = replace(load(), media=settings(section))
    async with Client(build_server(config)) as client:
        answer = await refused(client, "generate_image", prompt="x")

    assert "unknown media api" in answer, "the sentence, not a raw exception"


def test_the_size_check_accepts_the_three_ways_it_is_written() -> None:
    assert check_size("1024*1024") == "1024*1024"
    assert check_size("1024x1024") == "1024x1024"
    assert check_size("") == "", "empty means the model's own default"
    with pytest.raises(ToolError, match="too large"):
        check_size("8192*8192")


# --- the config's four steps --------------------------------------------------


def test_a_provider_entry_has_to_say_what_it_speaks_and_where() -> None:
    """Refused at load, which is the opposite of v1 and right for where this
    parsing happens: v1 read the section at call time and had to skip a bad
    entry, where here the loader is the judge."""
    with pytest.raises(ConfigError, match="needs `api`"):
        _media(
            {"providers": {"p": {"base_url": "https://x", "models": []}}},
            MediaSettings(),
        )
    with pytest.raises(ConfigError, match="needs `base_url`"):
        _media({"providers": {"p": {"api": "a", "models": []}}}, MediaSettings())
    with pytest.raises(ConfigError, match="needs `models`"):
        _media(
            {"providers": {"p": {"api": "a", "base_url": "https://x"}}}, MediaSettings()
        )
    with pytest.raises(ConfigError, match="`kind` must be one of"):
        _media(
            {
                "providers": {
                    "p": {
                        "api": "a",
                        "base_url": "https://x",
                        "models": [{"model": "m", "kind": "sound"}],
                    }
                }
            },
            MediaSettings(),
        )
    with pytest.raises(ConfigError, match="not a kind of media"):
        _media({"defaults": {"sound": "p/m"}}, MediaSettings())


def test_a_base_url_loses_its_trailing_slash() -> None:
    assert settings().providers["test"].base_url == "https://example.test/api/v1"


def test_a_reference_can_name_a_provider_a_model_or_neither() -> None:
    media = settings()
    assert media.resolve("image", "test/img-pro")[1].model == "img-pro"
    assert media.resolve("image", "img-pro")[0].name == "test", (
        "a bare name is searched"
    )
    assert media.resolve("image")[1].model == "img", "then `defaults:`"
    assert media.resolve("tts")[1].model == "say", "then the first of that kind"


def test_a_model_of_the_wrong_kind_is_refused_before_the_call() -> None:
    """A video model handed an image prompt is a paid failure with a
    provider-side message; the config already says which one draws pictures."""
    with pytest.raises(ConfigError, match="is a video model, not image"):
        settings().resolve("image", "test/vid")
    with pytest.raises(ConfigError, match="Unknown model"):
        settings().resolve("image", "test/nope")
    with pytest.raises(ConfigError, match="Unknown media provider"):
        settings().resolve("image", "nope/img")
    with pytest.raises(ConfigError, match="No asr model configured"):
        MediaSettings().resolve("asr")


def test_a_stale_default_falls_through_to_the_first_of_the_kind() -> None:
    """A config that stops answering because one word went stale is the failure
    this prevents; what it allows is visible in the answer's own model."""
    media = settings(
        {"defaults": {"image": "test/gone"}, "providers": SECTION["providers"]}
    )

    assert media.resolve("image")[1].model == "img"


def test_the_kinds_are_what_a_provider_declares() -> None:
    assert settings().providers["test"].kinds() == ("image", "video", "tts", "asr")


def test_parameters_merge_with_the_call_winning() -> None:
    assert merge_params({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}
    assert merge_params(None, {"b": 3}) == {"b": 3}


# --- the adapters, against a fake transport -----------------------------------


def dashscope() -> DashScopeAIGCAdapter:
    return DashScopeAIGCAdapter(settings().providers["test"])


def test_an_unknown_api_is_refused_at_the_first_call() -> None:
    with pytest.raises(MediaAdapterError, match="unknown media api"):
        adapters.create_adapter(
            settings()
            .providers["test"]
            .__class__(name="p", api="brand-new", base_url="https://x")
        )


@pytest.mark.asyncio
async def test_an_image_request_has_the_vendors_shape(monkeypatch, tmp_path) -> None:
    adapter = dashscope()
    seen: dict[str, Any] = {}

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        seen.update(method=method, url=url, body=json_body)
        return {
            "output": {
                "choices": [{"message": {"content": [{"image": "https://cdn/x.png"}]}}]
            }
        }

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(
        adapter._saver, "save_url", _saver_returning(tmp_path / "x.png")
    )

    answer = await adapter.generate_image(
        model="img", prompt="a cat", size="1024*1024", outputs_dir=tmp_path
    )

    assert answer == str(tmp_path / "x.png")
    assert seen["url"].endswith("/services/aigc/multimodal-generation/generation")
    assert seen["body"]["model"] == "img"
    assert seen["body"]["input"]["messages"][0]["content"] == [{"text": "a cat"}]
    assert seen["body"]["parameters"] == {"size": "1024*1024"}


def _saver_returning(path: Path):
    async def save(url: str, kind: str, ext: str = "", *, outputs_dir: Path) -> Path:
        return path

    return save


@pytest.mark.asyncio
async def test_a_video_submits_asynchronously_and_polls(monkeypatch, tmp_path) -> None:
    """The header is what asks for a job rather than a wait, and the poll is
    what turns it into a file — with the deadline the caller gave it."""
    adapter = dashscope()
    calls: list[tuple[str, str, dict | None]] = []
    answers = [
        {"output": {"task_id": "t-1", "task_status": "PENDING"}},
        {"output": {"task_status": "RUNNING"}},
        {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn/v.mp4"}},
    ]

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        calls.append((method, url, extra_headers))
        return answers[len(calls) - 1]

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(adapters.asyncio, "sleep", _no_sleep())
    monkeypatch.setattr(
        adapter._saver, "save_url", _saver_returning(tmp_path / "v.mp4")
    )

    answer = await adapter.generate_video(
        model="vid",
        prompt="bounce",
        outputs_dir=tmp_path,
        deadline_s=60,  # timeouts: the value this test is built around, not a budget
    )

    assert answer == str(tmp_path / "v.mp4")
    assert calls[0][1].endswith("/services/aigc/video-generation/video-synthesis")
    assert calls[0][2] == {"X-DashScope-Async": "enable"}
    assert calls[1][0] == "GET" and calls[1][1].endswith("/tasks/t-1")


def _no_sleep():
    async def sleep(_seconds: float) -> None:
        return None

    return sleep


@pytest.mark.asyncio
async def test_a_job_that_fails_says_which_status(monkeypatch, tmp_path) -> None:
    adapter = dashscope()
    answers = [
        {"output": {"task_id": "t-2"}},
        {"output": {"task_status": "FAILED", "message": "no such model"}},
    ]

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        return answers.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(adapters.asyncio, "sleep", _no_sleep())

    with pytest.raises(MediaAdapterError, match="failed: no such model"):
        await adapter.generate_video(model="vid", prompt="x", outputs_dir=tmp_path)


@pytest.mark.asyncio
async def test_speech_is_a_flat_body_and_may_come_back_inline(
    monkeypatch, tmp_path
) -> None:
    """Two shapes from one endpoint: a URL, or the bytes base64'd."""
    adapter = dashscope()
    seen: dict[str, Any] = {}
    encoded = base64.b64encode(b"RIFF").decode()

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        seen.update(url=url, body=json_body)
        return {"output": {"audio": {"data": encoded}}}

    monkeypatch.setattr(adapter, "_request", fake_request)

    answer = await adapter.text_to_speech(
        model="say", text="hi", voice="v1", outputs_dir=tmp_path
    )

    assert seen["url"].endswith("/services/audio/tts/SpeechSynthesizer")
    assert seen["body"] == {"model": "say", "input": {"text": "hi", "voice": "v1"}}
    assert Path(answer).read_bytes() == b"RIFF"


@pytest.mark.asyncio
async def test_a_transcript_reads_three_answer_shapes(monkeypatch, tmp_path) -> None:
    """`output.text`, a segmented `sentence`, and a `choices` list — the endpoint
    has all three, so all three are read."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    adapter = dashscope()
    answers = [
        {"output": {"text": "one"}},
        {"output": {"sentence": {"text": "two"}}},
        {"output": {"choices": [{"message": {"content": [{"text": "three"}]}}]}},
    ]

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        return answers.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)

    assert await adapter.transcribe_audio(model="hear", audio_path=audio) == "one"
    assert await adapter.transcribe_audio(model="hear", audio_path=audio) == "two"
    assert await adapter.transcribe_audio(model="hear", audio_path=audio) == "three"


@pytest.mark.asyncio
async def test_an_audio_format_the_adapter_does_not_know_is_refused(tmp_path) -> None:
    """The data URI's header decides how the far end decodes the bytes, so a
    guess is bytes nobody can read."""
    audio = tmp_path / "a.xyz"
    audio.write_bytes(b"?")

    with pytest.raises(MediaAdapterError, match="not an audio format"):
        await dashscope().transcribe_audio(model="hear", audio_path=audio)


@pytest.mark.asyncio
async def test_an_image_format_the_adapter_does_not_know_is_refused(tmp_path) -> None:
    """The same rule `slife2.libs.turn.attachments` follows for the same question.

    That module used to guess `image/png` for an unknown suffix while this one
    refused it — two routes to a provider disagreeing about one thing.  Both
    refuse now, and this pins the side that was already right.
    """
    picture = tmp_path / "diagram.svg"
    picture.write_bytes(b"<svg/>")

    with pytest.raises(MediaAdapterError, match="not an image format"):
        await dashscope().generate_image(
            model="img", prompt="edit this", image=picture, outputs_dir=tmp_path
        )


@pytest.mark.asyncio
async def test_an_http_error_carries_its_status(monkeypatch) -> None:
    """`401` and `500` are the same sentence to a person only if the number is
    thrown away."""
    adapter = dashscope()

    class Response:
        status_code = 401
        text = "unauthorized"

    class Client:
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            return Response()

    monkeypatch.setattr(adapter, "_ensure_client", _client_returning(Client()))

    with pytest.raises(MediaAdapterError) as caught:
        await adapter._request("GET", "https://x")
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_a_payload_that_is_not_base64_is_a_failure_not_a_traceback(
    monkeypatch, tmp_path
) -> None:
    """**Both wires decode a field they did not write, and neither `_run`.**

    `base64.b64decode` raises `binascii.Error` — a `ValueError` — for a payload
    that is not base64 and `TypeError` for a field that is not a string at all,
    and `slife2.plugins.media.server._run` converts only `NotImplementedError`,
    `MediaAdapterError` and `FileNotFoundError`.  So a vendor answering with a
    truncated `b64_json` (or an `audio.data` that is an object) reached the model
    as a raw traceback instead of the sentence §1.5 promises —
    exactly the class of answer `_json_object` exists to eliminate one line up.
    """
    images = OpenAICompatAdapter(settings().providers["test"])
    monkeypatch.setattr(
        images, "_ensure_client", _posting({"data": [{"b64_json": "not base64!"}]})
    )
    with pytest.raises(MediaAdapterError, match="b64_json"):
        await images.generate_image(model="img", prompt="x", outputs_dir=tmp_path)

    speech = dashscope()

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        return {"output": {"audio": {"data": {"nested": "not a string"}}}}

    monkeypatch.setattr(speech, "_request", fake_request)
    with pytest.raises(MediaAdapterError, match="audio.data"):
        await speech.text_to_speech(model="say", text="hi", outputs_dir=tmp_path)


@pytest.mark.asyncio
async def test_a_provider_with_no_key_sends_no_authorization_header() -> None:
    """**A local media endpoint needs no key, and `Bearer ` is not "no key".**

    Unlike the LLM and embeddings servers, this plugin's `api_key` defaults to
    `""` (`MediaProviderSettings`) — a media provider is commonly a server on
    this machine, where a key is not part of the config at all.  What the header
    used to be in that case was `Authorization: Bearer ` — a credential *present
    and empty*, which a gateway is entitled to read as a bad credential and
    answer 401 to, so the local endpoint that wanted no key was refused by a
    header nobody asked for.  A key that is configured still travels, and a
    `${...}` that did not resolve is still refused rather than sent.
    """
    unkeyed = MediaProviderSettings(
        name="local", api="openai-images", base_url="https://x/v1"
    )
    adapter = OpenAICompatAdapter(unkeyed)
    client = await adapter._ensure_client()
    try:
        assert "authorization" not in {name.lower() for name in client.headers}, (
            "an empty credential was sent as a header nobody asked for"
        )
    finally:
        await adapter.close()

    keyed = OpenAICompatAdapter(settings().providers["test"])
    client = await keyed._ensure_client()
    try:
        assert client.headers["authorization"] == "Bearer k"
    finally:
        await keyed.close()

    unresolved = OpenAICompatAdapter(
        MediaProviderSettings(
            name="p",
            api="openai-images",
            base_url="https://x/v1",
            api_key_ref="${NO_SUCH_VARIABLE_ANYWHERE}",
        )
    )
    with pytest.raises(MediaAdapterError, match="did not resolve"):
        await unresolved._ensure_client()


@pytest.mark.asyncio
async def test_a_video_that_outruns_its_timeout_names_the_job_it_left_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The tool's own words and the code have to say the same thing.**

    The description promised that a render outrunning `timeout` "answers with
    the job's id", and nothing implements that: the call *fails*, with the id
    inside the refusal — which is where it is worth having, because the only
    thing the id is for is asking the provider about a render this call has
    stopped waiting for.  A model told the id arrives as an answer would look
    for a file that does not exist and pay for the render twice; so the
    description says what happens, and this asserts both halves.
    """
    async with Client(server_for()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}
    description = listed["generate_video"].description or ""
    assert "answers with the job's id" not in description, (
        "the description still promises an answer the code does not give"
    )
    assert "provider's job id in the message" in description

    adapter = dashscope()

    async def fake_request(
        method: str, url: str, *, extra_headers=None, json_body=None
    ):
        return {"output": {"task_id": "t-1", "task_status": "RUNNING"}}

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(adapters.asyncio, "sleep", _no_sleep())
    config = replace(load(), media=settings())

    async with Client(build_server(config, adapters={"test": adapter})) as client:
        answer = await refused(client, "generate_video", prompt="x", timeout=0.05)

    assert answer.startswith("the video call failed:"), (
        "a timeout is a refusal, not a path"
    )
    assert "t-1" in answer, "the job the provider is still running is not named"


def _client_returning(client: Any):
    async def ensure() -> Any:
        return client

    return ensure


def _posting(body: Any, status_code: int = 200, text: str = "{}"):
    """A client whose one POST answers with *body* as JSON.

    The shapes a provider can answer with that are *not* the one the adapter
    expects — which is the whole of what the guard under test is about — so the
    body is the parameter rather than a whole client spelled out per test.
    """

    class Response:
        def __init__(self) -> None:
            self.status_code = status_code
            self.text = text

        @staticmethod
        def json() -> Any:
            return body

    class Client:
        async def post(self, url: str, json: dict[str, Any]) -> Any:
            return Response()

    return _client_returning(Client())


@pytest.mark.asyncio
async def test_the_images_only_adapter_refuses_the_other_three(
    monkeypatch, tmp_path
) -> None:
    """Four capabilities behind two wires, and the refusal names the gap.

    `match` on each, because the subject of the docstring above is the
    *sentence*: a bare `pytest.raises(NotImplementedError)` passes for any
    `NotImplementedError` from any capability, so an adapter that answered
    `raise NotImplementedError` with nothing in it would satisfy this — and the
    plugin builds its `this provider cannot make a <capability>: …`
    answer out of exactly this text (`test_a_capability_the_adapter_lacks_names_
    the_adapter`).  Each one names its own capability, which is the half a model
    needs: what it cannot have, not merely that something is missing.
    """
    adapter = OpenAICompatAdapter(
        MediaProviderSettings(name="o", api="openai-images", base_url="https://x/v1")
    )
    with pytest.raises(NotImplementedError, match="video"):
        await adapter.generate_video(model="m", prompt="x", outputs_dir=tmp_path)
    with pytest.raises(NotImplementedError, match="speech"):
        await adapter.text_to_speech(model="m", text="x", outputs_dir=tmp_path)
    with pytest.raises(NotImplementedError, match="transcription"):
        await adapter.transcribe_audio(model="m", audio_path=tmp_path / "a.wav")


@pytest.mark.asyncio
async def test_an_openai_image_may_come_back_as_bytes(monkeypatch, tmp_path) -> None:
    adapter = OpenAICompatAdapter(settings().providers["test"])
    encoded = base64.b64encode(b"\x89PNG").decode()

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"data": [{"b64_json": encoded}]}

    class Client:
        async def post(self, url: str, json: dict[str, Any]) -> Any:
            assert url.endswith("/images/generations")
            assert json["model"] == "img"
            return Response()

    monkeypatch.setattr(adapter, "_ensure_client", _client_returning(Client()))

    answer = await adapter.generate_image(model="img", prompt="x", outputs_dir=tmp_path)

    assert Path(answer).read_bytes() == b"\x89PNG"


@pytest.mark.asyncio
async def test_an_images_answer_that_is_not_json_is_a_failure_not_a_traceback(
    monkeypatch, tmp_path
) -> None:
    """A `200` whose body will not parse is a `MediaAdapterError` — the one thing
    `_run` turns into a sentence — not a `ValueError` escaping the tool."""
    adapter = OpenAICompatAdapter(settings().providers["test"])

    class Response:
        status_code = 200
        text = "<html>gateway timeout</html>"

        @staticmethod
        def json() -> Any:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class Client:
        async def post(self, url: str, json: dict[str, Any]) -> Any:
            return Response()

    monkeypatch.setattr(adapter, "_ensure_client", _client_returning(Client()))

    with pytest.raises(MediaAdapterError, match="not JSON"):
        await adapter.generate_image(model="img", prompt="x", outputs_dir=tmp_path)


@pytest.mark.asyncio
async def test_an_images_answer_of_an_unexpected_shape_is_a_failure(
    monkeypatch, tmp_path
) -> None:
    """A body that is JSON but not an object must not `.get` its way to an
    `AttributeError` out of the tool."""
    adapter = OpenAICompatAdapter(settings().providers["test"])
    monkeypatch.setattr(adapter, "_ensure_client", _posting([{"b64_json": "eA=="}]))

    with pytest.raises(MediaAdapterError, match="unexpected shape"):
        await adapter.generate_image(model="img", prompt="x", outputs_dir=tmp_path)


@pytest.mark.asyncio
async def test_an_images_answer_with_no_usable_item_is_a_failure(
    monkeypatch, tmp_path
) -> None:
    """A `data` that is not a list of objects must not index its way to an
    `IndexError` or a `TypeError` out of the tool."""
    adapter = OpenAICompatAdapter(settings().providers["test"])
    monkeypatch.setattr(adapter, "_ensure_client", _posting({"data": ["nope"]}))

    with pytest.raises(MediaAdapterError, match="no image in the answer"):
        await adapter.generate_image(model="img", prompt="x", outputs_dir=tmp_path)


@pytest.mark.asyncio
async def test_saving_an_artifact_goes_off_the_event_loop(
    monkeypatch, tmp_path
) -> None:
    """A generated artifact is megabytes — a video, an audio file — so
    `save_bytes` writes it through `asyncio.to_thread`, the way the reads on the
    send side already hop (`_data_uri`, `_reference`)."""
    adapter = dashscope()
    hopped: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def spy(func: Any, *args: Any) -> Any:
        hopped.append(func)
        return await real_to_thread(func, *args)

    monkeypatch.setattr(adapters.asyncio, "to_thread", spy)
    encoded = base64.b64encode(b"RIFF").decode()

    async def fake_request(method, url, *, extra_headers=None, json_body=None):
        return {"output": {"audio": {"data": encoded}}}

    monkeypatch.setattr(adapter, "_request", fake_request)

    answer = await adapter.text_to_speech(model="say", text="hi", outputs_dir=tmp_path)

    assert Path(answer).read_bytes() == b"RIFF"
    assert any(getattr(func, "__name__", "") == "write_bytes" for func in hopped)
