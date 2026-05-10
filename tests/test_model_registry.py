"""Plan 38 PR-A: tests for the model registry (transport ↔ id consistency)."""

from __future__ import annotations

import pytest

from quikode.model_registry import MODELS, ModelSpec, _validate, get_model


def test_every_entry_has_consistent_transport_fields() -> None:
    for name, spec in MODELS.items():
        if spec.transport == "codex_direct":
            assert spec.codex_profile is not None, f"{name}: codex_direct requires codex_profile"
            assert spec.claude_model_id is None, f"{name}: codex_direct forbids claude_model_id"
            assert spec.schema_enforcement == "cli_native"
        elif spec.transport == "codex_litellm":
            assert spec.codex_profile is not None
            assert spec.claude_model_id is None
            assert spec.schema_enforcement == "client_side"
        elif spec.transport == "claude":
            assert spec.claude_model_id is not None
            assert spec.codex_profile is None
            assert spec.schema_enforcement == "cli_native"


def test_known_codex_profiles_are_present() -> None:
    """The seven existing codex profiles in `~/.codex/config.toml` map to
    seven entries in MODELS."""
    expected_profiles = {
        "gpt5",
        "codex",
        "glm-zai",
        "glm-wafer",
        "minimax",
        "deepseek",
        "qwen",
    }
    actual = {spec.codex_profile for spec in MODELS.values() if spec.codex_profile}
    assert expected_profiles <= actual


def test_get_model_known_name() -> None:
    spec = get_model("gpt-5.5")
    assert spec.transport == "codex_direct"
    assert spec.codex_profile == "gpt5"


def test_glm_zai_falls_back_to_wafer_then_codex_on_quota() -> None:
    spec = get_model("GLM-5.1-zai")
    assert spec.quota_fallbacks == ("GLM-5.1-wafer", "gpt-5.3-codex")


def test_get_model_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError) as exc:
        get_model("totally-bogus-model")
    assert "totally-bogus-model" in str(exc.value)


def test_validate_codex_direct_requires_profile() -> None:
    with pytest.raises(ValueError, match="codex_profile"):
        _validate(
            ModelSpec(
                name="bad",
                transport="codex_direct",
                schema_enforcement="cli_native",
                codex_profile=None,
            )
        )


def test_validate_codex_direct_forbids_claude_model_id() -> None:
    with pytest.raises(ValueError, match="claude_model_id"):
        _validate(
            ModelSpec(
                name="bad",
                transport="codex_direct",
                schema_enforcement="cli_native",
                codex_profile="gpt5",
                claude_model_id="oops",
            )
        )


def test_validate_claude_requires_model_id() -> None:
    with pytest.raises(ValueError, match="claude_model_id"):
        _validate(
            ModelSpec(
                name="bad",
                transport="claude",
                schema_enforcement="cli_native",
                claude_model_id=None,
            )
        )


def test_validate_codex_litellm_requires_client_side() -> None:
    with pytest.raises(ValueError, match="client_side"):
        _validate(
            ModelSpec(
                name="bad",
                transport="codex_litellm",
                schema_enforcement="cli_native",  # wrong tier for litellm
                codex_profile="glm-zai",
            )
        )


def test_validate_codex_direct_requires_cli_native() -> None:
    with pytest.raises(ValueError, match="cli_native"):
        _validate(
            ModelSpec(
                name="bad",
                transport="codex_direct",
                schema_enforcement="client_side",  # wrong tier for direct
                codex_profile="gpt5",
            )
        )


def test_claude_opus_in_registry() -> None:
    spec = get_model("claude-opus-4-7")
    assert spec.transport == "claude"
    assert spec.claude_model_id == "claude-opus-4-7[1m]"
