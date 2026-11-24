"""The `model_*` tools: the model config, edited where it is resolved.

The four model config tools live in the process that turns the provider table
and its `default:` into a conversation's model — the edit and the read that
honours it are one process, so a switch needs no reload protocol between them
("哪个 plugin 用，哪个 plugin 管").  The hub reaches them because this server is one of
`Config.plugins()`, the same way it reaches `_check_new_input`.

**Everything here reads the file, never `Config.providers`.**  A held provider
has been through `resolve_secret`, so a listing built from one would print the
operator's live key into the conversation and the transcript; `read_section` is
the `${VAR}` the file holds, and none of these answers shows even that.

The tools take the core's narrow surface — `loops`, `current()` and
`this_home()` — because those are the three facts that make this family the
agent server's: the file a *new* conversation starts from, the home whose
conversations are the caller's, and the table a switch moves.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host import configfile
from slife2.libs.host.config import Config, ConfigError, first_reference
from slife2.libs.host.model_config import (
    ModelEntry,
    load_model,
    load_provider,
    model_entry,
    show_api_key,
    to_reference,
)
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.protocol import describe

from .core import AgentCore


def _providers() -> dict[str, dict[str, Any]]:
    """The `providers:` entries as the file has them — secrets unresolved."""
    return configfile.read_providers()


def _active_default(providers: dict[str, dict[str, Any]]) -> str:
    """The model reference the system runs on, read off the file.

    `providers.default` when the file has one, else the first model of the
    first provider —
    `slife2.libs.host.config.first_reference`'s rule, read from the file rather
    than from `Config` so that it reflects an edit this process has just made.
    **A bare provider is the reference its first model makes**, which is
    `Config.resolve`'s reading: the loader accepts a bare provider, so
    comparing the raw scalar here made the "it is the default model" guard
    compare two spellings of one model and never fire.
    """
    return first_reference(providers, configfile.read_default())


def _model_ref(ref: str) -> tuple[str, str]:
    """`provider/model`, split and stripped — the one reference check every
    model tool shares, so `deepseek / x` is the same reference to all of
    them, and the same refusal when it is not one."""
    provider, slash, model = ref.strip().partition("/")
    provider, model = provider.strip(), model.strip()
    if not slash or not provider or not model:
        raise ToolError(f"`{ref}` is not a model reference; use `provider/model`.")
    return provider, model


def _no_such_model(ref: str) -> ToolError:
    """The refusal for a reference the config does not define — one sentence
    for every tool that can discover it."""
    return ToolError(f"`{ref}` is not a model this config defines.")


def _model_config(ref: str, providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One model's whole entry, validated by the canonical config object.

    **The file is read raw and judged, not trusted.**  A hand-written typo
    — `imgae`, `temprature`, an unknown `compat` key — is the failure the
    shared model exists to catch, so this raises rather than returning the
    shape the file happens to have.  A literal API key is written as `""`:
    the reference is what a caller restates, and the secret never travels.
    """
    provider_name, model_id = _model_ref(ref)
    entry = providers.get(provider_name)
    if not isinstance(entry, dict):
        raise _no_such_model(ref)
    try:
        provider = load_provider(entry, where=f"providers.{provider_name}")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    models = entry.get("models")
    spec = (
        next(
            (
                one
                for one in models
                if isinstance(one, dict) and one.get("model") == model_id
            ),
            None,
        )
        if isinstance(models, list)
        else None
    )
    if spec is None:
        raise _no_such_model(ref)
    try:
        model = load_model(spec, where=f"providers.{provider_name}")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "provider": {
            **provider.model_dump(),
            "api_key": show_api_key(provider.api_key),
        },
        "model": model.model_dump(),
    }


async def _ensure_backend(core: AgentCore, api: str) -> dict[str, Any]:
    """Start the model server for an api nothing was serving.

    **The one case a re-read cannot cover.**  The agent and the backends
    follow the file, but a protocol with no provider at boot has no process
    to follow it — so the first provider added for one is a server that has
    to exist before the model can be reached.  `launcher.ensure` is the same
    call the launcher makes, so the process is started, recorded and reused
    exactly as one from a cold start.

    Off the event loop, because `probe` refuses to run inside one by design
    (`slife2.libs.host.launcher`); and on the common path `ensure` probes first, so a
    model added beside a provider whose backend is already up costs one
    round trip and starts nothing.

    Answers `{"api", "up", "detail"}` — the protocol, whether it has a
    backend now, and the reason when it has not.  **A start that failed is a
    value and not an exception**, so the model is told its model was written
    and its backend was not: the edit stands either way, and a restart is
    what retries.
    """
    from slife2.libs.host import launcher

    try:
        outcome = await asyncio.to_thread(
            launcher.ensure,
            launcher.spec_for_api(core.current(), api),
            config_path=configfile.config_path(),
        )
    except Exception as exc:  # noqa: BLE001 - a start that raised is not a crash
        return {
            "api": api,
            "up": False,
            "detail": f"its server could not be started ({exc}); a restart will retry",
        }
    if outcome.ok:
        return {"api": api, "up": True, "detail": outcome.detail}
    return {
        "api": api,
        "up": False,
        "detail": f"it did not come up — "
        f"{outcome.detail or 'it did not answer'}; a restart will retry",
    }


def register_tools(mcp: FastMCP, core: AgentCore) -> None:
    """Serve the `model_*` family on the server that resolves the model."""

    @mcp.tool(name="model_list", meta=FOR_THE_MODEL)
    async def model_list() -> dict[str, Any]:
        """List the models this config defines.

        Reads the config file, so it is what the next conversation would use.
        Each row carries `ref`, the `provider/model` reference `model_get` and
        `model_set` take.

        Returns:
            `models`, one row per model: `ref` (the `provider/model` reference),
            `provider`, `model`, `name`, whether it is the `default`, and
            whether it reasons or reads images, under `marks`; `context_window`,
            the window its entry declares — `null` for a row the loader would
            refuse; and `max_tokens`, `0` when the config does not say.  Plus
            `total`, how many models there are, and `default`, the reference the
            system runs on — `null` when the config names none.
        """
        try:
            providers = _providers()
            active = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        # **No `limit`/`offset`, and the bound is what decides it.**  Every other
        # listing here answers a log — turns, runs — which grows for as long as
        # the system is used, so it pages.  This one answers the config file: a
        # handful of models an operator wrote down, read whole in a fraction of
        # the token budget, where a page would be two arguments the model has to
        # reason about for a list it can hold in one look.
        models: list[dict[str, Any]] = []
        for provider, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            specs = entry.get("models")
            for spec in specs if isinstance(specs, list) else []:
                if not isinstance(spec, dict):
                    continue
                model = str(spec.get("model") or "")
                marks = []
                if f"{provider}/{model}" == active:
                    marks.append("default")
                if spec.get("reasoning"):
                    marks.append("reasoning")
                if "image" in (spec.get("input") or []):
                    marks.append("image")
                models.append(
                    {
                        "ref": f"{provider}/{model}",
                        "provider": provider,
                        "model": model,
                        "name": str(spec.get("name") or model),
                        "marks": marks,
                        # The file's own value: a model the loader refuses has
                        # no window, and a zero here would read as one.
                        "context_window": spec.get("context_window"),
                        "max_tokens": spec.get("max_tokens") or 0,
                    }
                )
        return {"models": models, "total": len(models), "default": active or None}

    @mcp.tool(name="model_get", meta=FOR_THE_MODEL)
    async def model_get(ref: str) -> dict[str, Any]:
        """Read one model's whole entry, by `provider/model`.

        The answer is the canonical entry — `config.provider` and
        `config.model`, every writable field present — so `model_set` takes the
        same object back. `default` says whether `providers.default` names this
        model.

        `config.provider.api_key` is the `${VAR}` reference, never the
        resolved key; empty means the file has a literal key that is not shown. A hand-written typo in the entry is refused here rather than
        answered as if it were the truth.

        Args:
            ref: The model, as `provider/model`.
        """
        # **The canonical reference, for `_model_ref`'s reason.**  Comparing
        # the raw spelling to `providers.default` made `local / small` a model
        # that is not the default — the entry was found by `_model_config`,
        # which splits and strips, and the answer beside it said `false`.
        provider, model = _model_ref(ref)
        ref = f"{provider}/{model}"
        providers = _providers()
        return {
            "ref": ref,
            "config": _model_config(ref, providers),
            "default": ref == _active_default(providers),
        }

    @mcp.tool(name="model_set", meta=FOR_THE_MODEL)
    async def model_set(ref: str, config: ModelEntry) -> dict[str, Any]:
        """Add or replace one model — the whole entry, and no patch.

        Every field is required: a field left out is a schema error, not a
        silent default. Read `model_get`, change what you mean to change, and
        pass the whole `config` back.

        `config.provider` is shared with the provider's other models: `api`,
        `base_url` and `api_key` belong to the provider, not this model.
        `api_key` is a `${VAR}` reference; empty keeps an existing provider's
        key, and is refused for a new provider.

        The answer is the entry as written — the same `config` shape
        `model_get` gives — plus `created`/`provider_created`, and
        `ready`/`error` when this call started the provider's backend.

        Args:
            ref: The model, as `provider/model`.
            config: The whole entry — the `config` object `model_get` returned.
        """
        provider, model = _model_ref(ref)
        if config.model.model != model:
            raise ToolError(
                f"`{ref}` names the model {model!r}, but `config.model.model` "
                f"is {config.model.model!r}; they must match."
            )
        try:
            api_key = to_reference(config.provider.api_key)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        providers = _providers()
        existing = providers.get(provider)
        creating = not isinstance(existing, dict)
        if creating and not (config.provider.base_url and api_key):
            raise ToolError(
                f"`{provider}` is a new provider, so it needs `base_url` and "
                f"`api_key` — the key as a `${{VAR}}` reference, so it stays out "
                f"of the file."
            )
        if not config.provider.base_url:
            raise ToolError(
                f"`{provider}` needs `base_url` — the endpoint its models are "
                f"reached at."
            )
        entry_api = config.provider.api
        known = {
            str(entry.get("api"))
            for entry in providers.values()
            if isinstance(entry, dict) and entry.get("api")
        }
        before = [
            one
            for one in (
                ((existing or {}).get("models") if isinstance(existing, dict) else None)
                or []
            )
            if isinstance(one, dict)
        ]
        had_model = any(one.get("model") == model for one in before)

        def change(current: dict[str, Any]) -> dict[str, Any]:
            entry = dict(current)
            entry["api"] = config.provider.api
            entry["base_url"] = config.provider.base_url
            if api_key:
                entry["api_key"] = api_key
            spec = model_entry(config.model)
            # Siblings are read from the entry `update_entry` handed us, not
            # from the earlier read: a concurrent edit between the two must not
            # be dropped by this write.  An existing model is replaced in
            # place, so `set(get(...))` does not reorder the list; a new one
            # goes first, where a reader looks for it.
            models = [
                one for one in (current.get("models") or []) if isinstance(one, dict)
            ]
            if any(one.get("model") == model for one in models):
                entry["models"] = [
                    spec if one.get("model") == model else one for one in models
                ]
            else:
                entry["models"] = [spec, *models]
            return entry

        try:
            configfile.update_entry("providers", provider, change)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        answer: dict[str, Any] = {
            "ref": f"{provider}/{model}",
            "config": _model_config(f"{provider}/{model}", _providers()),
            "created": not had_model,
            "provider_created": creating,
        }
        # Only when this call is what made the protocol reachable: a provider
        # for an api that was already configured is served by a server that has
        # been up since boot, and reporting on it here would be this tool
        # answering about a process it never touched.
        if entry_api not in known:
            outcome = await _ensure_backend(core, entry_api)
            answer["ready"] = bool(outcome.get("up"))
            if not outcome.get("up"):
                answer["error"] = str(outcome.get("detail") or "")
        return answer

    @mcp.tool(name="model_remove", meta=FOR_THE_MODEL)
    async def model_remove(ref: str) -> dict[str, Any]:
        """Remove one model, named `provider/model`.

        The model stays at its provider, and the provider goes too when that was
        its last model. Refused for the default model — switch to another first
        — and for one a running conversation is on, whose model is fixed when it
        starts.

        Args:
            ref: The model, as `provider/model`.

        Returns:
            `ref`, and `provider_removed`: whether the provider's whole entry
            went with it, which happens when this was its last model.
        """
        provider, model = _model_ref(ref)
        ref = f"{provider}/{model}"
        try:
            providers = _providers()
            active = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        entry = providers.get(provider)
        specs = (entry or {}).get("models")
        specs = specs if isinstance(specs, list) else []
        if not any(
            isinstance(one, dict) and one.get("model") == model for one in specs
        ):
            raise _no_such_model(ref)
        # **The config-wide guard comes first, deliberately.**  With one model
        # the fallback makes it the default too, so both refusals are true — and
        # "it is the last model" is the deeper reason, the one that says what
        # would actually go wrong.
        total = sum(
            len([one for one in entry.get("models") or [] if isinstance(one, dict)])
            for entry in providers.values()
            if isinstance(entry, dict)
        )
        if total == 1:
            raise ToolError(
                f"`{ref}` is the last model in the config; without it the file "
                f"would name no model for slife2 to run on. `model_set` another "
                f"first."
            )
        if ref == active:
            raise ToolError(
                f"`{ref}` is the default model; `model_switch` to another first."
            )
        # **This home's conversations, and this home's only.**  The loop table is
        # keyed by `(home, agent, subagent)` because one process serves several
        # homes, and `current()` above read the *caller's* file — so a loop filed
        # under another home is a conversation this call knows nothing about, and
        # refusing on its behalf would be a false refusal that also printed
        # somebody else's conversation names into an error the model reads.
        here = core.this_home()
        holders = [
            describe((one.agent, one.subagent))
            for key, one in core.loops.items()
            if key[0] == here and one.model == ref
        ]
        if holders:
            raise ToolError(
                f"`{ref}` is the model a conversation is running on "
                f"({', '.join(holders)}); `model_switch` to another one first — "
                f"it moves the conversations running on this one with it."
            )

        def change(current: dict[str, Any]) -> dict[str, Any] | None:
            kept = [
                one
                for one in (current.get("models") or [])
                if not (isinstance(one, dict) and one.get("model") == model)
            ]
            if not kept:
                # A provider with no models is one the loader refuses, so its
                # entry goes with its last model rather than being left empty.
                return None
            return {**current, "models": kept}

        try:
            configfile.update_entry("providers", provider, change)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        return {"ref": ref, "provider_removed": len(specs) == 1}

    @mcp.tool(name="model_switch", meta=FOR_THE_MODEL)
    async def model_switch(ref: str) -> dict[str, Any]:
        """Make `provider/model` the model this system runs on, at once.

        Writes `providers.default:` in the config file, and moves every
        conversation this server is holding onto it: each keeps its history and
        runs on the new model from its next turn. That is what makes it the way
        out of a model that has stopped answering. The model must be one the
        config defines.

        Args:
            ref: The model, as `provider/model`.

        Returns:
            `default` — the reference the system now runs on — `previous`, what
            the default said before, and `moved`, how many conversations that
            were already running this server moved onto it from their next turn.
        """
        provider, model = _model_ref(ref)
        ref = f"{provider}/{model}"
        try:
            providers = _providers()
            old = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        entry = providers.get(provider)
        specs = (entry or {}).get("models")
        specs = specs if isinstance(specs, list) else []
        if not any(
            isinstance(one, dict) and one.get("model") == model for one in specs
        ):
            raise _no_such_model(ref)

        # What the conversations below are moved *onto*, and it is not the same
        # question as the judge inside the writer.  The default is a scalar the
        # model's own settings do not depend on, so resolving the reference a
        # moment before the write reads the same entry the write leaves behind —
        # which is what lets the move be made from an answer `resolve` gave
        # rather than from the file's raw dicts, whose rules about `input:` and
        # about a bare provider name are `Config.resolve`'s and are not worth a
        # second spelling here.
        try:
            name, endpoint, settings = core.current().resolve(ref)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        def resolves(config: Config) -> None:
            """The writer's judge: `load` accepts any string, `resolve` does not."""
            config.resolve(ref)

        try:
            configfile.set_default(ref, check=resolves)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        # **The conversations move with it, and this is the half that makes the
        # tool worth having.**  Writing the default alone changes what the *next*
        # conversation starts on — so switching away from a model that has
        # stopped answering, which is the whole reason to do this, would leave
        # the window being answered (or not) by the model it was trying to
        # leave.  The loop is reloaded for exactly that reason, and it is done
        # here rather than by the caller because this process is the one
        # holding the loops: the edit and the read that honours it are one
        # process, which is the argument that puts these four tools on the
        # agent server at all.
        #
        # From the next turn, never mid-turn.  `loop_for` resolves the backend
        # once, when a turn starts, so a turn already in flight finishes on the
        # model it began on — and the record of what answered it was taken at
        # that same boundary (`Outcome.model`).  Everything the conversation
        # reaches its model with moves, images included: `accepts_images` is
        # what `attach_image` refuses against.
        # **Only this home's conversations move, and that is a boundary rather
        # than a filter.**  The table is keyed by `(home, agent, subagent)`
        # because one process serves every home on the machine, and the
        # reference just resolved came out of the *caller's* `slife2.yaml` — so
        # moving another home's loop would point it at a model its own config may
        # not even name, and its store would then record a foreign model.  The
        # home is read here, inside the call, which is the only place it is the
        # caller's (`this_home`).
        here = core.this_home()
        mine = [loop for key, loop in core.loops.items() if key[0] == here]
        reference = f"{name}/{settings.model}"
        for loop in mine:
            loop.model = reference
            loop.provider = name
            loop.api = endpoint.api
            loop.model_id = settings.model
            loop.accepts_images = settings.accepts_images
            loop.prompt_window = settings.prompt_window

        return {"default": reference, "previous": old or None, "moved": len(mine)}
