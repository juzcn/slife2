"""slife2-media — generation: an image, a video, speech, and a transcript.

v1's `media` plugin, and the four capabilities are the whole of it:

    generate_image      draw a picture from a prompt
    generate_video      render a video, optionally from one
    text_to_speech      say something out loud
    transcribe_audio    write down what an audio file says

**A plugin, by §1.1's own table.**  It holds a provider's key and speaks to
somebody else's endpoint, so it is a process rather than a library — the same
row of that table as the model backends and the embeddings server, and for the
same reason: a library would put the key in every importer.

**One section, several vendors, and the config says which speaks what.**  The
`media:` section is a provider table like `providers:` or `embeddings:`, with one
difference that shapes this module: an *entry* here declares what each of its
models is for (`kind: image`), because "which model draws pictures" is a fact
about a model rather than about a vendor.  `MediaSettings.resolve` is the four
step search that answers it, and `slife2/plugins/media/adapters.py` is the translation
layer — so this file never learns a vendor's name either.

**The four tools carry no addressing of their own.**  A caller names a model only
to override the configured default, and a `folder` only to sort its own work;
everything else — endpoint, key, the model's own parameters — is the config's,
which is what makes "switch to the cheap image model" a config edit rather than
a new call.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import (
    Config,
    ConfigError,
    MediaProviderSettings,
    MediaSettings,
)
from slife2.libs.plugin.audience import FOR_THE_MODEL, request_client
from slife2.libs.plugin.protocol import house_server, serve_plugin
from slife2.plugins.media.adapters import (
    MediaAdapter,
    MediaAdapterError,
    create_adapter,
    media_dir,
    merge_params,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-media"

#: This server's key in the config's `servers:` table, and the section it owns.
CONFIG_KEY = "media"

INSTRUCTIONS = (
    "Generation: images, video, speech and transcripts, from the `media:` "
    "providers. Each tool names the file it wrote, or says what refused it. "
    "`generate_video` can take minutes; a long render times out on the wait "
    "while the job keeps running at the provider."
)

# ── The one bound a caller can be wrong about ────────────────────────────────
# An image size is the argument a model gets *plausibly* wrong — "2048x2048" for
# a model whose floor is 768 — and the vendor's own refusal arrives after the
# call has been paid for.  v1 checked it here, with its numbers.

#: Smallest and largest total pixels the vendor accepts (about 768²..4096²).
MIN_PIXELS = 589_824
MAX_PIXELS = 16_777_216

#: `1024*1024`, `1024x1024`, `1024 X 1024` — the three ways this gets written.
_SIZE = re.compile(r"^(\d+)\s*[xX*]\s*(\d+)$")


def check_size(size: str) -> str:
    """A size the caller wrote, or a sentence saying why it is not one.

    Empty passes: "whatever the model defaults to" is a valid thing to want, and
    the config's own `params` may already say it.
    """
    if not size.strip():
        return ""
    matched = _SIZE.match(size.strip())
    if matched is None:
        raise ToolError(
            f"[refused] {size!r} is not a size. Write it as `WIDTH*HEIGHT`, "
            f"two whole numbers — `1024*1024`."
        )
    pixels = int(matched.group(1)) * int(matched.group(2))
    if not MIN_PIXELS <= pixels <= MAX_PIXELS:
        direction = "small" if pixels < MIN_PIXELS else "large"
        raise ToolError(
            f"[refused] {size!r} is too {direction}: {pixels} pixels, and this "
            f"provider takes {MIN_PIXELS}..{MAX_PIXELS} (about 768x768 to "
            f"4096x4096)."
        )
    return size.strip()


class Media:
    """The config, and one adapter per provider that has been used.

    **Adapters are built on first use and kept**, because each holds an HTTP
    client with a connection pool: rebuilding one per call would handshake for
    every image.  The map is keyed by provider name, so two calls that name the
    same provider share a client and two providers never do — which is also what
    keeps one vendor's `Authorization` header off another's connection.
    """

    def __init__(
        self, config: Config, *, adapters: dict[str, MediaAdapter] | None = None
    ) -> None:
        self._settings: MediaSettings = config.media
        self._adapters: dict[str, MediaAdapter] = dict(adapters or {})

    def adapter(self, provider: MediaProviderSettings) -> MediaAdapter:
        """The adapter for one provider, built on first use."""
        found = self._adapters.get(provider.name)
        if found is None:
            found = create_adapter(provider)
            self._adapters[provider.name] = found
        return found

    def resolve(self, kind: str, model: str) -> tuple[MediaProviderSettings, Any]:
        """`(provider, model entry)` for one call, or a refusal to read.

        `ConfigError` is turned into a `ToolError` here rather than at each tool:
        "no image model configured" is the same sentence whichever of the four
        asked, and the config's own words for it are already the right ones.
        """
        try:
            return self._settings.resolve(kind, model)
        except ConfigError as exc:
            raise ToolError(f"[refused] {exc}") from exc

    def output(self, ctx: Context, folder: str) -> Path:
        """Where this call's artifact goes: `agents/<agent>/media[/folder]/`.

        The agent comes from the call, like every other per-agent fact in this
        system: one process serves every agent, so "whose work is this" is
        something only the caller can say — and a call that does not say is
        refused rather than written under somebody else's name.
        """
        found = request_client(ctx)
        if not found or not found[0]:
            raise ToolError(
                "[refused] this call did not say which conversation it is for, "
                "so there is no agent directory to write into."
            )
        try:
            return media_dir(found[0], folder)
        except MediaAdapterError as exc:
            raise ToolError(f"[refused] {exc}") from exc

    async def close(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 — a close on the way out
                logger.debug("media adapter did not close cleanly", exc_info=True)


def build_server(
    config: Config, *, adapters: dict[str, MediaAdapter] | None = None
) -> FastMCP:
    """Build the media server.

    `adapters` is the seam, and it is the one worth having: every test of the
    four tools drives a fake adapter — v1's tests do the same — so what is under
    test is the *tool* half (which provider, which model, which parameters,
    which folder) with no HTTP anywhere.  The adapters' own shapes are tested
    against a fake transport in `tests/test_media.py`, which is the other half.
    """
    media = Media(config, adapters=adapters)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await media.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool(name="generate_image", meta=FOR_THE_MODEL)
    async def generate_image(
        ctx: Context,
        prompt: str,
        model: str = "",
        size: str = "",
        image: str = "",
        folder: str = "",
    ) -> str:
        """Draw an image from a description, and say where it was written.

        The picture is a file on this machine: copy it, or save it into the
        cabinet, to keep it.

        Args:
            prompt: What the image should be.
            model: `provider/model`, or a bare name. Empty uses the configured
                default for images.
            size: `WIDTH*HEIGHT`, e.g. `1024*1024`. Empty uses the model's own
                default.
            image: A picture to work from — a local path, an `http(s)` URL or a
                `data:` URI. For models that take a reference image.
            folder: A folder inside the media directory to write into. Empty
                writes at the top.
        """
        wanted = check_size(size)
        provider, entry = media.resolve("image", model)
        params = merge_params(entry.params, {"size": wanted} if wanted else {})
        output = await _run(
            "image",
            lambda: media.adapter(provider).generate_image(
                model=entry.model,
                prompt=prompt,
                size=wanted,
                image=image or None,
                outputs_dir=media.output(ctx, folder),
                extra_params=params,
            ),
        )
        return f"Image written to {output}"

    @mcp.tool(name="generate_video", meta=FOR_THE_MODEL)
    async def generate_video(
        ctx: Context,
        prompt: str,
        model: str = "",
        image: str = "",
        resolution: str = "",
        ratio: str = "",
        duration: int = 0,
        folder: str = "",
        # The model's own argument rather than a budget of ours: it is the
        # schema's default, the tool reads it, and there is nothing here to
        # route through the registry.
        timeout: float = 0,  # timeouts: the model's number, not ours
    ) -> str:
        """Render a video from a description, and say where it was written.

        A render takes minutes; one that outruns `timeout` answers with the
        job's id, and the render carries on at the provider.

        Args:
            prompt: What happens in the video.
            model: `provider/model`, or a bare name. Empty uses the configured
                default for video.
            image: A first frame to animate, as a local path. For
                image-to-video models.
            resolution: `720P`, `1080P`. Empty uses the model's own default.
            ratio: `16:9`, `9:16`, `1:1`. Empty uses the model's own default.
            duration: Seconds. Zero uses the model's own default.
            folder: A folder inside the media directory to write into.
            timeout: How long to wait, in seconds. Zero or less uses the
                default.
        """
        provider, entry = media.resolve("video", model)
        given: dict[str, Any] = {}
        if resolution:
            given["resolution"] = resolution
        if ratio:
            given["ratio"] = ratio
        if duration:
            given["duration"] = duration
        params = merge_params(entry.params, given)
        output = await _run(
            "video",
            lambda: media.adapter(provider).generate_video(
                model=entry.model,
                prompt=prompt,
                image=image or None,
                outputs_dir=media.output(ctx, folder),
                extra_params=params,
                deadline_s=timeout if timeout > 0 else None,
            ),
        )
        return f"Video written to {output}"

    @mcp.tool(name="text_to_speech", meta=FOR_THE_MODEL)
    async def text_to_speech(
        ctx: Context, text: str, model: str = "", voice: str = "", folder: str = ""
    ) -> str:
        """Say something out loud, into an audio file, and say where it is.

        Args:
            text: What to say.
            model: `provider/model`, or a bare name. Empty uses the configured
                default for speech.
            voice: Which voice, by the provider's own name. Empty uses the
                model's default.
            folder: A folder inside the media directory to write into.
        """
        provider, entry = media.resolve("tts", model)
        output = await _run(
            "speech",
            lambda: media.adapter(provider).text_to_speech(
                model=entry.model,
                text=text,
                voice=voice or entry.voice,
                outputs_dir=media.output(ctx, folder),
                extra_params=dict(entry.params),
            ),
        )
        return f"Audio written to {output}"

    @mcp.tool(name="transcribe_audio", meta=FOR_THE_MODEL)
    async def transcribe_audio(ctx: Context, path: str, model: str = "") -> str:
        """Write down what an audio file says, and answer with the text.

        Args:
            path: The audio file, as a local path.
            model: `provider/model`, or a bare name. Empty uses the configured
                default for transcription.
        """
        if not path.strip():
            raise ToolError("[refused] no audio file was named.")
        provider, entry = media.resolve("asr", model)
        text = await _run(
            "transcript",
            lambda: media.adapter(provider).transcribe_audio(
                model=entry.model,
                # Not expanded here: every filesystem call belongs in the
                # adapter, where it goes off the event loop.
                audio_path=Path(path),
                extra_params=dict(entry.params),
            ),
        )
        return text

    return mcp


async def _run(noun: str, call: Callable[[], Awaitable[str]]) -> str:
    """One adapter call, with a provider's refusal turned into a sentence.

    **`call` is a factory, not the coroutine it produces.**  `media.adapter` is
    where `create_adapter`'s `MediaAdapterError` for an unknown `api:` is raised,
    and evaluating `media.adapter(provider).generate_image(…)` as an argument to
    this function ran the resolution *outside* the guard below.  A config with
    `api: typo` loads cleanly — the loader only requires the key to be present —
    and then failed at the first call as a raw exception instead of the
    `[failed] …` sentence §1.5 promises.  A zero-argument factory is what moves
    the resolution inside the `try`.

    **The three failures are told apart**, because a person does three different
    things about them: an adapter that cannot do this at all is a config choice
    to revisit, a provider that answered badly is worth retrying, and a missing
    file is the caller's own typo.  All three reach the model as a `ToolError`,
    which is this system's word for a tool that failed — v1 returned `"Error: …"`
    as text, and a refusal the transcript reads as a success is the failure mode
    that convention has.
    """
    try:
        return await call()
    except NotImplementedError as exc:
        raise ToolError(
            f"[refused] this provider cannot make a {noun}: {exc}. Another "
            f"`api:` (or another model of the right kind) would."
        ) from exc
    except MediaAdapterError as exc:
        raise ToolError(f"[failed] {noun}: {exc}") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"[refused] {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        note=lambda config: (
            f"{len(config.media.providers)} media provider(s)"
            if config.media.providers
            else "no `media:` section — the tools will refuse"
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
