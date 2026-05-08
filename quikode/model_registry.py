"""Plan 38 PR-A: model → CLI-transport registry.

Roles bind to MODELS, never to CLI names. Roles never reference a CLI by
name. The model-name encodes which CLI shim transports the call and
whether schema enforcement is CLI-native (Tier 1) or client-side (Tier
2). The role/agent layer (`quikode.agent_registry`) consumes this
registry to dispatch a `ModelSpec` to the matching transport.

Adding a new model is a one-line edit to `MODELS`. Adding a new
provider is: register in `~/.codex/litellm_config.yaml`, add a codex
profile in `~/.codex/config.toml`, then add a `MODELS` entry.

Validation runs at import time: every entry's `transport` ↔
`codex_profile` / `claude_model_id` consistency is checked, and any
inconsistency raises `ValueError` immediately so a misconfigured
registry can't ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelTransport = Literal["codex_direct", "codex_litellm", "claude"]
SchemaEnforcement = Literal["cli_native", "client_side"]


@dataclass(frozen=True)
class ModelSpec:
    """One row in the model registry.

    `transport` selects the CLI shim:
    - `codex_direct` — direct OpenAI Responses API via `codex exec`.
      Tier 1 (cli_native) schema enforcement: the CLI guarantees the
      `--output-last-message` payload conforms to `--output-schema`.
    - `codex_litellm` — codex CLI routed through the local litellm proxy.
      Tier 2 (client_side): litellm 1.83.10 drops `output_schema` during
      Responses → Chat translation, so the response is free text and the
      agent layer parses with `model_validate_json` + re-prompts once on
      `ValidationError`.
    - `claude` — `claude -p --output-format json --json-schema "$(...)"`.
      Tier 1 (cli_native): the envelope's `structured_output` is already
      schema-validated by the CLI.

    `codex_profile` is mandatory iff `transport` starts with `codex_`.
    `claude_model_id` is mandatory iff `transport == "claude"`.
    """

    name: str
    transport: ModelTransport
    schema_enforcement: SchemaEnforcement
    codex_profile: str | None = None
    claude_model_id: str | None = None


def _build_models() -> dict[str, ModelSpec]:
    """Construct + validate the registry. Called once at import time."""
    entries: list[ModelSpec] = [
        # OpenAI direct (Tier 1 — cli_native)
        ModelSpec(
            name="gpt-5.5",
            transport="codex_direct",
            schema_enforcement="cli_native",
            codex_profile="gpt5",
        ),
        ModelSpec(
            name="gpt-5.3-codex",
            transport="codex_direct",
            schema_enforcement="cli_native",
            codex_profile="codex",
        ),
        # Litellm-routed (Tier 2 — client_side)
        ModelSpec(
            name="GLM-5.1-zai",
            transport="codex_litellm",
            schema_enforcement="client_side",
            codex_profile="glm-zai",
        ),
        ModelSpec(
            name="GLM-5.1-wafer",
            transport="codex_litellm",
            schema_enforcement="client_side",
            codex_profile="glm-wafer",
        ),
        ModelSpec(
            name="MiniMax-M2.7",
            transport="codex_litellm",
            schema_enforcement="client_side",
            codex_profile="minimax",
        ),
        ModelSpec(
            name="DeepSeek-V4-Pro",
            transport="codex_litellm",
            schema_enforcement="client_side",
            codex_profile="deepseek",
        ),
        ModelSpec(
            name="Qwen3.5-397B-A17B",
            transport="codex_litellm",
            schema_enforcement="client_side",
            codex_profile="qwen",
        ),
        # Anthropic via claude CLI (Tier 1 — cli_native)
        ModelSpec(
            name="claude-opus-4-7",
            transport="claude",
            schema_enforcement="cli_native",
            claude_model_id="claude-opus-4-7[1m]",
        ),
        ModelSpec(
            name="claude-haiku-4-5",
            transport="claude",
            schema_enforcement="cli_native",
            claude_model_id="claude-haiku-4-5",
        ),
        ModelSpec(
            name="claude-sonnet-4-6",
            transport="claude",
            schema_enforcement="cli_native",
            claude_model_id="claude-sonnet-4-6",
        ),
    ]
    out: dict[str, ModelSpec] = {}
    for spec in entries:
        _validate(spec)
        if spec.name in out:
            raise ValueError(f"duplicate model name in registry: {spec.name!r}")
        out[spec.name] = spec
    return out


def _validate(spec: ModelSpec) -> None:
    """Enforce transport ↔ id-field consistency.

    `codex_*` transports require `codex_profile` and forbid `claude_model_id`.
    `claude` transport requires `claude_model_id` and forbids `codex_profile`.
    `cli_native` enforcement implies `codex_direct` or `claude`; `client_side`
    implies `codex_litellm`.
    """
    if spec.transport in ("codex_direct", "codex_litellm"):
        if not spec.codex_profile:
            raise ValueError(f"model {spec.name!r}: transport={spec.transport} requires codex_profile")
        if spec.claude_model_id is not None:
            raise ValueError(f"model {spec.name!r}: transport={spec.transport} forbids claude_model_id")
    elif spec.transport == "claude":
        if not spec.claude_model_id:
            raise ValueError(f"model {spec.name!r}: transport=claude requires claude_model_id")
        if spec.codex_profile is not None:
            raise ValueError(f"model {spec.name!r}: transport=claude forbids codex_profile")
    else:  # pragma: no cover — Literal covers the cases
        raise ValueError(f"model {spec.name!r}: unknown transport {spec.transport!r}")
    if spec.transport == "codex_direct" and spec.schema_enforcement != "cli_native":
        raise ValueError(f"model {spec.name!r}: codex_direct must declare schema_enforcement=cli_native")
    if spec.transport == "codex_litellm" and spec.schema_enforcement != "client_side":
        raise ValueError(f"model {spec.name!r}: codex_litellm must declare schema_enforcement=client_side")
    if spec.transport == "claude" and spec.schema_enforcement != "cli_native":
        raise ValueError(f"model {spec.name!r}: claude must declare schema_enforcement=cli_native")


MODELS: dict[str, ModelSpec] = _build_models()


def get_model(name: str) -> ModelSpec:
    """Look up a model by name. Raises `KeyError` with a helpful message
    when the name isn't registered (e.g. operator typo in
    `cfg.<role>_model`)."""
    try:
        return MODELS[name]
    except KeyError as e:
        known = ", ".join(sorted(MODELS.keys()))
        raise KeyError(f"unknown model name {name!r}; known models: {known}") from e


__all__ = [
    "MODELS",
    "ModelSpec",
    "ModelTransport",
    "SchemaEnforcement",
    "get_model",
]
