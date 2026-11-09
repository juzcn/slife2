"""The canonical config object for a provider and its models.

**This is the truth the file is judged against, not a second reading of it.**
`slife2.yaml` is an artifact a person edits: it may leave fields out, it may
carry a typo, and it may be older than this build.  The object here is the
shape the rest of the system means, and the loader validates the file into it
rather than inferring a shape from whatever the file happens to say.

The same object is what `model_get` returns and `model_set` accepts.  That is
the point of keeping it here rather than in either of those tools: a schema
that exists once cannot disagree with itself, and a hand-written typo is
refused at load, at `model_get`, and at `model_set` with the same sentence.

**Every field is required here, and the loader supplies the defaults.**  The
file may omit `reasoning`, `input`, `compat` and the rest; `load_model` fills
the canonical default before validation, so the object a caller sees is always
complete.  The tools do not fill anything: a `model_set` that leaves a field
out is a schema error, not a silent reset.  The two behaviours are the same
object; only the input side differs.

**`compat` is nested here although the runtime model flattens it.**  The file
spells it nested, the model-facing object spells it nested, and
`ModelSettings` is a projection of this object rather than a second definition
of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The wire protocols a provider may name.  `slife2.libs.host.config.API_BACKENDS`
#: is derived from the plugins, and a test holds the two sets equal; this module
#: cannot import it without a cycle (`config` imports this one to parse).
API_PROTOCOLS = (
    "openai-completions",
    "anthropic-messages",
    "openai-responses",
)

Api = Literal["openai-completions", "anthropic-messages", "openai-responses"]
Modality = Literal["text", "image"]
Thinking = Literal["enabled", "disabled", "omit"]
StreamUsage = Literal["omit"]
CacheControl = Literal["ephemeral", "5m", "1h"]


class Compat(BaseModel):
    """The wire quirks one model's endpoint needs, nested as the file spells them.

    Every field is required and nullable: an absent value is `None`, which is
    how the file's "say nothing" reaches the adapters.  The model-facing set
    therefore has to state every quirk, and the loader fills the all-`None`
    object for a file that omits `compat` entirely.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    thinking: Thinking | None = Field(
        description="`enabled`, `disabled`, or `omit` for a gateway that "
        "rejects the standard thinking field while reasoning anyway."
    )
    stream_usage: StreamUsage | None = Field(
        description="`omit` for a gateway that rejects OpenAI's "
        "`stream_options`; null sends it."
    )
    store: bool | None = Field(
        description="Whether the Responses API may keep the response; null "
        "sends nothing."
    )
    cache_control: CacheControl | None = Field(
        description="Anthropic's automatic prompt cache: `ephemeral`, `5m` or "
        "`1h`; null sends nothing."
    )


class ProviderConfig(BaseModel):
    """One provider entry's own fields — everything except its `models:` list."""

    model_config = ConfigDict(strict=True, extra="forbid")

    api: Api = Field(
        description="The wire protocol: `openai-completions`, "
        "`anthropic-messages` or `openai-responses`."
    )
    base_url: str = Field(
        description="The endpoint the provider's models are reached at."
    )
    api_key: str = Field(
        description="A `${VAR}` or `keyring:` reference, as the file writes it. "
        "Empty keeps an existing provider's key; a new provider needs one."
    )


class ModelConfig(BaseModel):
    """One model entry, whole — the object `model_get` returns under `model`."""

    model_config = ConfigDict(strict=True, extra="forbid")

    model: str = Field(
        min_length=1,
        description="The API's model name, and the id in `provider/model`.",
    )
    name: str = Field(description="The display name; empty falls back to `model`.")
    reasoning: bool = Field(
        description="The model thinks natively, and its reasoning is kept on the turn."
    )
    input: list[Modality] = Field(description="What it reads: `text` and/or `image`.")
    context_window: int = Field(description="Its context window, in tokens.")
    max_tokens: int | None = Field(
        description="Its output limit, in tokens; null uses the provider's default."
    )
    temperature: float | None = Field(
        description="Sampling temperature; null uses the provider's default."
    )
    top_p: float | None = Field(
        description="Nucleus sampling; null uses the provider's default."
    )
    compat: Compat = Field(
        description="Wire quirks the endpoint needs; every key is required, and "
        "null means the file says nothing."
    )


class ModelEntry(BaseModel):
    """A provider and one model, the object `model_set` accepts and `model_get` returns."""

    model_config = ConfigDict(strict=True, extra="forbid")

    provider: ProviderConfig = Field(
        description="The provider-level half, shared with its other models."
    )
    model: ModelConfig = Field(description="This model's own entry.")


#: What a file that omits one `compat` key means.  The loader fills these
#: under a partial `compat:` mapping, because a file may name only the quirk
#: its endpoint needs.
COMPAT_DEFAULTS: dict[str, Any] = {
    "thinking": None,
    "stream_usage": None,
    "store": None,
    "cache_control": None,
}

#: What a file that omits a model field means.  The loader merges these under
#: the raw entry; the tool takes the resulting `ModelConfig` whole, so a
#: `model_set` can never inherit one of these by accident.
MODEL_DEFAULTS: dict[str, Any] = {
    "name": "",
    "reasoning": False,
    "input": ["text"],
    "context_window": 0,
    "max_tokens": None,
    "temperature": None,
    "top_p": None,
    "compat": COMPAT_DEFAULTS,
}

#: What a provider entry that omits `api_key:` means.
PROVIDER_DEFAULTS: dict[str, Any] = {"api_key": ""}

#: A `str` that looks like an environment variable name.  A model that writes
#: `OPENAI_API_KEY` means the reference, not a literal key; wrapping it is the
#: same courtesy `rest_api_set` already extends.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_MISSING = object()

#: Model fields whose `null` in YAML means "the field was not written", which
#: the loader turns into the canonical default.  `reasoning`, `max_tokens`,
#: `temperature` and `top_p` are absent here deliberately: a written `null`
#: there is either refused (the switch) or a real value (the numbers).
_NULL_IS_ABSENT = ("name", "input", "compat", "context_window")


def _error(where: str, exc: ValidationError) -> ValueError:
    """The first validation failure, in the config's own sentence.

    **The field path is the part a model acts on**, so it is always there, and
    the offending value is appended when Pydantic kept it — a refusal that says
    `providers.p.api: Input should be ...` is one the caller can fix without
    opening the file.
    """
    first = exc.errors()[0]
    path = ".".join(str(part) for part in first.get("loc", ()))
    # **Only an enum or a switch echoes its value.**  Pydantic puts the whole
    # parent mapping in `input` for a missing field, and that can carry a
    # literal API key; an unknown key's value can be one too.  The field path
    # plus the message is enough to fix either, and a literal error's value is
    # a protocol, a modality or a quirk — never a credential.
    got = first.get("input", _MISSING)
    shown = first.get("type") in ("literal_error", "bool_type") and got is not _MISSING
    suffix = f" (got {got!r})" if shown else ""
    where = f"{where}.{path}" if path else where
    return ValueError(f"{where}: {first['msg']}{suffix}")


def load_provider(raw: Any, *, where: str) -> ProviderConfig:
    """One provider entry's own fields, validated against the canonical object.

    `models:` is removed before validation because it belongs to the list this
    provider owns, not to the provider's own shape.  A key this build does not
    know is **refused** rather than ignored: a typo in a hand-written file is
    the thing this module exists to catch.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a mapping")
    data = {str(key): value for key, value in raw.items() if key != "models"}
    if data.get("api_key") is None:
        data.pop("api_key", None)
    data = {**PROVIDER_DEFAULTS, **data}
    try:
        return ProviderConfig.model_validate(data)
    except ValidationError as exc:
        raise _error(where, exc) from exc


def load_model(raw: Any, *, where: str) -> ModelConfig:
    """One model entry, validated and completed with the canonical defaults.

    The file may leave out `name`, `input`, `compat`, `context_window` and the
    numeric fields; this is the one place that turns those absences into the
    values the rest of the system means.  *This* is why `model_set` can require
    every field without making an old file invalid: loading and setting are the
    same object, with the defaults applied on one side only.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: each model must be a mapping")
    data = {str(key): value for key, value in raw.items()}
    for key in _NULL_IS_ABSENT:
        if data.get(key) is None:
            data.pop(key, None)
    data = {**MODEL_DEFAULTS, **data}
    compat = data.get("compat")
    if isinstance(compat, Mapping):
        # A partial `compat:` names only the quirk its endpoint needs; the
        # other keys are the canonical `None`.  An empty string is how an older
        # file spells "say nothing"; the canonical object spells it `None`, and
        # the adapters read both alike.
        data["compat"] = {
            **COMPAT_DEFAULTS,
            **{
                str(key): (None if value == "" else value)
                for key, value in compat.items()
            },
        }
    try:
        return ModelConfig.model_validate(data)
    except ValidationError as exc:
        raise _error(where, exc) from exc


def model_entry(model: ModelConfig) -> dict[str, Any]:
    """A model's YAML entry, from the canonical object.

    **A value equal to the loader's default is written as absent**, so
    `model_set(model_get(...))` leaves the file's shape alone instead of
    splattering every default across it.  The state is the same either way;
    this is the serializer's half of "the object is the truth".
    """
    spec: dict[str, Any] = {"model": model.model}
    if model.name:
        spec["name"] = model.name
    if model.reasoning:
        spec["reasoning"] = True
    if list(model.input) != MODEL_DEFAULTS["input"]:
        spec["input"] = list(model.input)
    if model.context_window:
        spec["context_window"] = model.context_window
    if model.max_tokens is not None:
        spec["max_tokens"] = model.max_tokens
    if model.temperature is not None:
        spec["temperature"] = model.temperature
    if model.top_p is not None:
        spec["top_p"] = model.top_p
    compat = {
        key: value
        for key, value in model.compat.model_dump().items()
        if value is not None
    }
    if compat:
        spec["compat"] = compat
    return spec


def show_api_key(value: str) -> str:
    """The key as a model may see it: the reference, or nothing.

    A `${VAR}`/`keyring:` reference is not the secret and is what a full-set
    has to restate.  A literal written into the file is the operator's secret,
    so it is shown as `""`; `model_set` treats `""` as "leave the provider's
    key alone", which is the one field where that is the safe answer.
    """
    value = (value or "").strip()
    if value.startswith("${") or value.startswith("keyring:"):
        return value
    return ""


def to_reference(value: str) -> str:
    """A model-written credential reference, normalized; the secret itself is refused.

    `OPENAI_API_KEY` means `${OPENAI_API_KEY}` — the same courtesy
    `rest_api_set` extends.  Anything else that is not already a reference is
    a literal key, and a model-facing tool must not write one into the file.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("${") or value.startswith("keyring:"):
        return value
    if _ENV_NAME.match(value):
        return f"${{{value}}}"
    raise ValueError(
        "api_key must be a `${VAR}` or `keyring:` reference — not the secret itself"
    )
