"""How a media provider is spoken to: one contract, and the adapters that keep it.

v1's shape, and it is the shape the problem has: **one endpoint family per
adapter**, and a provider entry names which one it speaks (`api:`).  What a tool
asks for is a *capability* — draw this, render this, say this, transcribe this —
and what an adapter does is translate that into one vendor's HTTP.  So a second
vendor is a class and a registry entry, and nothing above this module learns its
name.

**No SDK, and no new dependency.**  Both adapters speak plain HTTP over
`httpx2`, which this project already depends on — v1's do the same, and it is
why the port needed no change to `pyproject.toml`: a media endpoint is three or
four JSON calls, a poll, and a download, and an SDK would be a dependency that
exists to spell them differently.

**Artifacts land in the agent's own directory**, and that is where this port
deliberately differs from v1.  v1 wrote them to the process's working directory;
here a daemon's working directory is `<data>/runtime/`, which this project
documents as *reconstructible — delete it and the next start rebuilds whatever
it needs*, so a generated image left there would be the one file guaranteed to
be lost.  What it is instead is the agent's own (`agents/<agent>/media/`):
generated work belongs to whoever asked for it.  The bytes are still work products rather than cabinet documents (v1's
distinction, kept): nothing is registered anywhere, and the answer is a path a
person can open.

**A failure is raised, not returned as text.**  v1 answered `"Error: …"`, which
was its convention for a tool failure; here the convention is `ToolError`
(`slife2.skills_server` states it: a refusal returned as text reaches the
transcript as a *success*).  So the adapter raises and the plugin decides.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx2

from slife2.paths import agent_dir

logger = logging.getLogger(__name__)

# ── Cadence and bounds ──────────────────────────────────────────────────────
# v1 kept these in a global timeouts registry (`slife.timeouts`) that every
# plugin read.  There is no such registry here — a number is a constant beside
# the call it paces — and the values are v1's, measured against the same
# providers: a render is slow, a connect is not, and an artifact is megabytes.

#: One request, from connect to last byte.  Generous on purpose: a synchronous
#: render is a long POST.
REQUEST_TIMEOUT = 180.0
CONNECT_TIMEOUT = 30.0
#: Downloading the artifact the provider hands back.
DOWNLOAD_TIMEOUT = 300.0
#: How long an asynchronous job (a video) may take before the wait gives up.
#: **The job keeps running on the provider's side** — the message says so — so
#: this bounds the *wait*, not the render.
DEADLINE_SECONDS = 1200.0
#: How often an asynchronous job is asked about.
POLL_SECONDS = 15.0

#: The artifacts' directory inside one agent's own.
MEDIA_DIR = "media"


def media_dir(agent: str, folder: str = "") -> Path:
    """Where an artifact goes: `agents/<agent>/media[/folder]/`.

    **Per agent**, because what it holds is what that agent generated in its own
    turns — and **contained**, which is not decoration: `folder` is a
    model-written string, and `folder="../../.."` would be a model choosing
    where on this machine to write a file.  Resolved before it is compared,
    refused rather than rewritten — a silently corrected path is a file
    somewhere other than where the model said (`slife2.cabinet_store.resolve`
    draws the same line).
    """
    root = agent_dir(agent) / MEDIA_DIR
    if not folder.strip():
        root.mkdir(parents=True, exist_ok=True)
        return root
    candidate = (root / folder).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise MediaAdapterError(
            f"{folder!r} is not a folder inside the media directory"
        )
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def stamp(moment: datetime | None = None) -> str:
    """A filename-safe local timestamp, to the second.

    `slife2.clock` writes this project's house format — `2026-10-11T14:20:00+08:00`
    — and a colon is not a character a Windows filename may hold, so this is the
    same instant in a shape a filesystem takes.  Seconds and six random hex
    digits are what make a collision impossible in practice; v1's naming, with
    its stamp respelled.
    """
    return f"{moment or datetime.now():%Y%m%d-%H%M%S}"


class MediaAdapterError(Exception):
    """A provider-side failure: an HTTP error, an error body, a failed job.

    Carries the status when there was one, because `401` and `500` are the same
    sentence to a person only if the number is thrown away.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _HttpClientMixin:
    """A lazy, lock-protected `httpx2` client, one per adapter.

    Both adapters carried verbatim copies of this in v1 — the same double-checked
    lock, the same headers — so it is one class here and the adapters inherit it.
    Call :meth:`_init_http_client` from ``__init__``.

    **One client for the adapter's life**, because a render is several calls to
    one host and a client per call would handshake for each of them.  The lock
    is what keeps two concurrent tool calls from building two clients.
    """

    #: Set by the concrete adapter's ``__init__`` (a `MediaProviderSettings`);
    #: the mixin reads `api_key` and `base_url` from it.
    _config: Any

    def _init_http_client(self) -> None:
        self._client: httpx2.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx2.AsyncClient:
        """The adapter's client, created on first use."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx2.AsyncClient(
                        timeout=httpx2.Timeout(
                            REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT
                        ),
                        headers={"Authorization": f"Bearer {self._config.api_key}"},
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@runtime_checkable
class MediaAdapter(Protocol):
    """What one provider's endpoint can be asked for.

    Every method returns a result string: the absolute path of the artifact for
    the three that produce one, and the transcript for `transcribe_audio`.
    A capability an endpoint does not have raises `NotImplementedError`, which
    the plugin turns into a sentence naming the adapter — the alternative,
    pretending, is a call that fails at the provider with a worse message.

    Keyword-only, so adding a parameter to one adapter cannot silently reorder
    another's.
    """

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        size: str = "",
        image: str | Path | None = None,
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str: ...

    async def generate_video(
        self,
        *,
        model: str,
        prompt: str,
        image: str | Path | None = None,
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
        deadline_s: float | None = None,
    ) -> str: ...

    async def text_to_speech(
        self,
        *,
        model: str,
        text: str,
        voice: str = "",
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str: ...

    async def transcribe_audio(
        self,
        *,
        model: str,
        audio_path: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str: ...

    async def close(self) -> None: ...


class ArtifactSaver:
    """Writes a generated artifact under the media directory and names it.

    **A work product, not a cabinet document.**  Nothing here registers the file
    with the cabinet or with any sharing plugin; the answer is a path, and
    putting the file somewhere durable is a separate call by whoever wants it
    (v1's distinction, kept).

    `kind` names the artifact in the log line and in an error message; it does
    not become a subdirectory, so a directory of generated things reads as the
    order they were made in.
    """

    def path(self, ext: str, outputs_dir: Path) -> Path:
        """A free path for one artifact — the time and a random suffix."""
        return outputs_dir / f"{stamp()}-{secrets.token_hex(3)}.{ext.lstrip('.')}"

    def save_bytes(self, data: bytes, kind: str, ext: str, outputs_dir: Path) -> Path:
        path = self.path(ext, outputs_dir)
        path.write_bytes(data)
        logger.info(
            "media_artifact_saved kind=%s path=%s bytes=%d", kind, path, len(data)
        )
        return path

    async def save_url(
        self, url: str, kind: str, ext: str = "", *, outputs_dir: Path
    ) -> Path:
        """Download *url* and store it.  Returns the local path.

        Its own client, not an adapter's: the download is to a CDN rather than
        to the provider, it carries no credential, and it follows redirects —
        which the API client must not do, because a redirect there would take
        the key somewhere else.
        """
        if not ext:
            ext = Path(url.split("?")[0]).suffix.lstrip(".") or "bin"
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT),
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content
        except httpx2.HTTPError as exc:
            raise MediaAdapterError(
                f"could not download the generated {kind}: {exc}"
            ) from exc
        return self.save_bytes(data, kind, ext, outputs_dir)


# ── Bailian / DashScope's AIGC endpoints ────────────────────────────────────

#: The three endpoint families under the base URL.  One per capability, because
#: the vendor serves each as its own service rather than as one generation API.
SYNC_PATH = "/services/aigc/multimodal-generation/generation"
VIDEO_PATH = "/services/aigc/video-generation/video-synthesis"
TTS_PATH = "/services/audio/tts/SpeechSynthesizer"

#: A `params` key that names the field a video model wants its reference image
#: in.  **Consumed here and never sent**: the vendor's image-to-video models
#: disagree about the field name (`image_url`, `first_frame_url`), so the config
#: says which, and a vendor that reads `params` would see the instruction.
IMAGE_FIELD_KEY = "image_field"
DEFAULT_IMAGE_FIELD = "image_url"

#: Audio suffixes as media types, pinned here rather than asked of `mimetypes`:
#: the data URI's header decides how the far end decodes the bytes, and a
#: platform that does not know `.m4a` would send `application/octet-stream` and
#: have the audio refused as not-audio.
AUDIO_MIME: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".amr": "audio/amr",
    ".silk": "audio/silk",
}

#: How much of a provider's error text is passed on.  Long enough to carry the
#: vendor's own words, short enough not to spend a context window doing it.
ERROR_CHARS = 500


class DashScopeAIGCAdapter(_HttpClientMixin):
    """Bailian's AIGC endpoints: image, video, TTS and ASR.

    The vendor this was written against, and the only adapter that implements
    all four capabilities — so it is also the one that shows what the contract
    costs: three envelopes (a chat-shaped one for generation, a flat one for
    speech, a data URI for transcription) and a poll for the jobs that take
    minutes.
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._init_http_client()
        self._saver = ArtifactSaver()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        extra_headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One call, with every way it can fail turned into one exception.

        Four failures and they read differently on purpose: a transport error,
        an HTTP status, a body that is not JSON, and a `200` carrying the
        vendor's own error envelope (`code`/`message`) instead of a result —
        the last being the one a client that only checks the status gets wrong.
        """
        client = await self._ensure_client()
        try:
            response = await client.request(
                method, url, headers=extra_headers, json=json_body
            )
        except httpx2.HTTPError as exc:
            raise MediaAdapterError(
                f"the media request failed ({method} {url}): {exc}"
            ) from exc
        if response.status_code >= 400:
            raise MediaAdapterError(
                f"the media API answered {response.status_code}: "
                f"{response.text[:ERROR_CHARS]}",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise MediaAdapterError(
                f"the media API answered with something that is not JSON: "
                f"{response.text[:200]}"
            ) from exc
        if isinstance(data, dict) and data.get("code"):
            raise MediaAdapterError(
                f"the media API returned an error: {data.get('code')} — "
                f"{data.get('message', '')}"
            )
        return data

    async def _generate(
        self,
        model: str,
        content: list[dict[str, Any]],
        parameters: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """The chat-shaped envelope, and the `output` inside it."""
        body = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": parameters,
        }
        data = await self._request(
            "POST",
            f"{self._config.base_url}{SYNC_PATH}",
            extra_headers=extra_headers,
            json_body=body,
        )
        output = data.get("output")
        if not isinstance(output, dict):
            raise MediaAdapterError(f"the answer has no output: {str(data)[:300]}")
        return output

    @staticmethod
    def _content(output: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            content = output["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaAdapterError(
                f"unexpected answer shape: {str(output)[:300]}"
            ) from exc
        if isinstance(content, str):
            return [{"text": content}]
        if not isinstance(content, list):
            raise MediaAdapterError(
                f"unexpected content type: {type(content).__name__}"
            )
        return [item for item in content if isinstance(item, dict)]

    async def _submit(
        self, model: str, data: dict[str, Any], params: dict[str, Any]
    ) -> str:
        """Start an asynchronous job, and answer with its id."""
        answer = await self._request(
            "POST",
            f"{self._config.base_url}{VIDEO_PATH}",
            extra_headers={"X-DashScope-Async": "enable"},
            json_body={"model": model, "input": data, "parameters": params},
        )
        task_id = (answer.get("output") or {}).get("task_id")
        if not task_id:
            raise MediaAdapterError(
                f"the job was accepted without an id: {str(answer)[:300]}"
            )
        logger.info("media_job_submitted model=%s task_id=%s", model, task_id)
        return str(task_id)

    async def _poll(self, task_id: str, deadline_s: float) -> dict[str, Any]:
        """Wait for a job, and answer with its output — or refuse, saying which.

        **A timeout is not a failure of the render**, and the message says so:
        the job is still running on the far side, and a caller who reads only
        "timed out" would start it again and pay twice.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        url = f"{self._config.base_url}/tasks/{task_id}"
        while True:
            if loop.time() - started > deadline_s:
                raise MediaAdapterError(
                    f"the job did not finish within {int(deadline_s)}s "
                    f"(provider task {task_id}); it may still be running there"
                )
            answer = await self._request("GET", url)
            output = answer.get("output") or {}
            status = str(output.get("task_status") or "")
            if status == "SUCCEEDED":
                return output
            if status in ("FAILED", "UNKNOWN", "CANCELED"):
                raise MediaAdapterError(
                    f"the job {status.lower()}: "
                    f"{output.get('message') or output.get('code') or 'no detail'}"
                )
            await asyncio.sleep(POLL_SECONDS)

    def _data_uri(self, path: Path) -> str:
        """A local file as a `data:` URI, with its media type.

        The suffix decides the type and an unknown one is refused rather than
        guessed: the far end decodes the payload by that header, so a wrong one
        is bytes nobody can read.
        """
        media_type = AUDIO_MIME.get(path.suffix.lower(), "")
        if not media_type:
            raise MediaAdapterError(
                f"{path.suffix or '(no suffix)'} is not an audio format this "
                f"adapter knows: {', '.join(sorted(AUDIO_MIME))}"
            )
        return (
            f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode()}"
        )

    def _reference(self, image: str | Path | None) -> str | None:
        """A reference image as something the far end can fetch: a URL or a URI.

        A local path becomes a data URI; a URL and a data URI pass through.  A
        path that is not there raises rather than being sent as a broken
        reference — the failure belongs where the caller can see it.
        """
        if image is None:
            return None
        if isinstance(image, Path):
            if not image.is_file():
                raise MediaAdapterError(f"no such reference image: {image}")
            return self._data_uri(image)
        if image.startswith(("http://", "https://", "data:")):
            return image
        path = Path(image).expanduser()
        if not path.is_file():
            raise MediaAdapterError(f"no such reference image: {path}")
        return self._data_uri(path)

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        size: str = "",
        image: str | Path | None = None,
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        content: list[dict[str, Any]] = [{"text": prompt}]
        if (reference := self._reference(image)) is not None:
            content.append({"image": reference})
        params = dict(extra_params or {})
        if size:
            params["size"] = size
        output = await self._generate(model, content, params)
        for item in self._content(output):
            if url := item.get("image"):
                path = await self._saver.save_url(
                    str(url), "image", outputs_dir=outputs_dir
                )
                return str(path)
        raise MediaAdapterError(f"no image in the answer: {str(output)[:300]}")

    async def generate_video(
        self,
        *,
        model: str,
        prompt: str,
        image: str | Path | None = None,
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
        deadline_s: float | None = None,
    ) -> str:
        params = dict(extra_params or {})
        field = str(params.pop(IMAGE_FIELD_KEY, DEFAULT_IMAGE_FIELD))
        data: dict[str, Any] = {"prompt": prompt}
        if (reference := self._reference(image)) is not None:
            data[field] = reference
        task_id = await self._submit(model, data, params)
        output = await self._poll(
            task_id, DEADLINE_SECONDS if deadline_s is None else deadline_s
        )
        url = output.get("video_url")
        if not url:
            results = output.get("results") or []
            if isinstance(results, list) and results:
                url = (results[0] or {}).get("url")
        if not url:
            raise MediaAdapterError(
                f"no video in the finished job: {str(output)[:300]}"
            )
        path = await self._saver.save_url(
            str(url), "video", "mp4", outputs_dir=outputs_dir
        )
        return str(path)

    async def text_to_speech(
        self,
        *,
        model: str,
        text: str,
        voice: str = "",
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        data: dict[str, Any] = {"text": text}
        if voice:
            data["voice"] = voice
        data.update(extra_params or {})
        answer = await self._request(
            "POST",
            f"{self._config.base_url}{TTS_PATH}",
            json_body={"model": model, "input": data},
        )
        output = answer.get("output") or {}
        audio = output.get("audio") or {}
        if url := audio.get("url"):
            path = await self._saver.save_url(
                str(url), "audio", outputs_dir=outputs_dir
            )
            return str(path)
        # The other shape the same endpoint answers with: the bytes inline,
        # base64'd.  Saving them as `.wav` is v1's choice and the vendor's
        # default format.
        if encoded := audio.get("data"):
            path = self._saver.save_bytes(
                base64.b64decode(encoded), "audio", "wav", outputs_dir
            )
            return str(path)
        raise MediaAdapterError(f"no audio in the answer: {str(output)[:300]}")

    async def transcribe_audio(
        self,
        *,
        model: str,
        audio_path: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        """Speech to text, out of the same chat-shaped endpoint.

        The audio rides as an `input_audio` data URI rather than being uploaded
        first, which is what this vendor's endpoint takes.  Three answer shapes
        are read because the endpoint has three: `output.text`, a segmented
        `output.sentence.text`, and a `choices` list like a chat answer.
        """
        if not audio_path.is_file():
            raise MediaAdapterError(f"no such audio file: {audio_path}")
        params = dict(extra_params or {})
        params.setdefault("format", "wav")
        params.setdefault("sample_rate", "16000")
        content: list[dict[str, Any]] = [
            {"type": "input_audio", "input_audio": {"data": self._data_uri(audio_path)}}
        ]
        output = await self._generate(model, content, params)
        if text := output.get("text"):
            return str(text)
        if text := (output.get("sentence") or {}).get("text"):
            return str(text)
        found = [
            str(item["text"]) for item in self._content(output) if item.get("text")
        ]
        if not found:
            raise MediaAdapterError(f"no transcript in the answer: {str(output)[:300]}")
        return "\n".join(found)


class OpenAICompatAdapter(_HttpClientMixin):
    """An OpenAI-compatible images endpoint, and nothing else.

    One capability, because that is the wire: `/images/generations` answers with
    a picture and this shape of endpoint has no video, speech or transcription
    service behind it.  A config pointing a `tts` model here is refused by
    `NotImplementedError` at the call, with a sentence naming the adapter, which
    is better than a 404 from the vendor and much better than a guess.
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._init_http_client()
        self._saver = ArtifactSaver()

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        size: str = "",
        image: str | Path | None = None,
        outputs_dir: Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        if image is not None:
            raise NotImplementedError(
                "an image to work from (this adapter draws from text alone)"
            )
        body: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        if size:
            body["size"] = size
        body.update(extra_params or {})
        client = await self._ensure_client()
        try:
            response = await client.post(
                f"{self._config.base_url}/images/generations", json=body
            )
        except httpx2.HTTPError as exc:
            raise MediaAdapterError(f"the image request failed: {exc}") from exc
        if response.status_code >= 400:
            raise MediaAdapterError(
                f"the image API answered {response.status_code}: "
                f"{response.text[:ERROR_CHARS]}",
                status_code=response.status_code,
            )
        data = response.json()
        items = data.get("data") or []
        if not items:
            raise MediaAdapterError(f"no image in the answer: {str(data)[:300]}")
        item = items[0]
        if encoded := item.get("b64_json"):
            path = self._saver.save_bytes(
                base64.b64decode(encoded), "image", "png", outputs_dir
            )
            return str(path)
        if url := item.get("url"):
            path = await self._saver.save_url(
                str(url), "image", outputs_dir=outputs_dir
            )
            return str(path)
        raise MediaAdapterError(
            f"the image has neither bytes nor a url: {str(item)[:300]}"
        )

    async def generate_video(self, **kwargs: Any) -> str:
        raise NotImplementedError("video (this adapter draws images only)")

    async def text_to_speech(self, **kwargs: Any) -> str:
        raise NotImplementedError("speech (this adapter draws images only)")

    async def transcribe_audio(self, **kwargs: Any) -> str:
        raise NotImplementedError("transcription (this adapter draws images only)")


#: `api:` value -> the adapter that speaks it.  **The one extension point**: a
#: new vendor is a class and a line here, and nothing above this module learns
#: its name.
ADAPTER_REGISTRY: dict[str, type] = {
    "dashscope-aigc": DashScopeAIGCAdapter,
    "openai-images": OpenAICompatAdapter,
}


def create_adapter(provider: Any) -> MediaAdapter:
    """The adapter for one provider entry.

    Raises:
        MediaAdapterError: If `api` names no adapter — refused here rather than
            at the first call, because "no adapter" and "the provider is down"
            are different problems and a config typo should not read like one.
    """
    found = ADAPTER_REGISTRY.get(provider.api)
    if found is None:
        known = ", ".join(sorted(ADAPTER_REGISTRY)) or "(none)"
        raise MediaAdapterError(
            f"unknown media api {provider.api!r}; known adapters: {known}"
        )
    return found(provider)


def merge_params(
    configured: dict[str, Any] | None, given: dict[str, Any] | None
) -> dict[str, Any]:
    """A model's configured parameters under the call's own.

    The call wins, and an absent value leaves the configured one alone — which
    is what makes `media:` a place to say "this model always renders 720P"
    without a tool growing a field per provider.  v1's rule.
    """
    return {**(configured or {}), **(given or {})}
