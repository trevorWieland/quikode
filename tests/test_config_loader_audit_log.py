"""Plan 38 PR-C: config-loader audit log for stale int overrides.

The trigger was commit d06cdcd bumping `subtask_doer_timeout_s` Field
default 1200 → 1800. Live workspaces' config.toml still pinned 1200,
silently capping doer calls at the prior ceiling. The drift was
invisible because no daemon-start log line surfaced the override.

`config_loader.from_toml` (via `load_config`) now walks every int
`Field` in `Config` and emits one INFO line per knob the toml is
overriding relative to the Field default. The next stale-default drift
shows up as soon as a daemon starts.
"""

from __future__ import annotations

import logging

from quikode import config_loader as loader_mod
from quikode.config import Config as ConfigCls
from quikode.config_loader import load_config


def _scaffold_workspace(tmp_path, *, toml_body: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    dag = tmp_path / "dag.json"
    dag.write_text('{"nodes":[]}')
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir()
    cfg_dir.joinpath("config.toml").write_text(toml_body.format(repo=repo, dag=dag))


def test_audit_log_fires_for_top_level_int_override(tmp_path, caplog):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "subtask_doer_timeout_s = 1200\n"
            "max_parallel = 3\n"  # matches Field default — must NOT log
        ),
    )
    caplog.set_level(logging.INFO, logger="quikode.config_loader")
    cfg = load_config(tmp_path)
    assert cfg.subtask_doer_timeout_s == 1200
    audit_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.INFO
    ]
    # Exactly one override-fire (the doer timeout). max_parallel matches
    # the Field default, so it is NOT in the audit log.
    matches = [line for line in audit_lines if "subtask_doer_timeout_s" in line]
    assert len(matches) == 1
    assert "1200" in matches[0]
    assert "1800" in matches[0]
    assert "overrides Field default" in matches[0]
    # max_parallel = 3 == Field default 3 → no audit line for it.
    assert not any("max_parallel" in line for line in audit_lines)


def test_audit_log_handles_subsection_int_override(tmp_path, caplog):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "[stacking]\n"
            "max_depth = 12\n"  # Field default 6
        ),
    )
    caplog.set_level(logging.INFO, logger="quikode.config_loader")
    load_config(tmp_path)
    audit_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.INFO
    ]
    matches = [line for line in audit_lines if "stacking.max_depth" in line]
    assert len(matches) == 1
    assert "12" in matches[0]
    assert "6" in matches[0]


def test_audit_log_silent_on_no_overrides(tmp_path, caplog):
    """A toml with no int overrides emits zero audit lines."""
    _scaffold_workspace(
        tmp_path,
        toml_body=('profile = "tanren"\nrepo_path = "{repo}"\ndag_path = "{dag}"\n'),
    )
    caplog.set_level(logging.INFO, logger="quikode.config_loader")
    load_config(tmp_path)
    audit_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.INFO
    ]
    assert audit_lines == []


def test_load_config_wires_subtask_same_signature_block_count(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "subtask_same_signature_block_count = 10\n"
        ),
    )
    cfg = load_config(tmp_path)
    assert cfg.subtask_same_signature_block_count == 10


def test_load_config_wires_subtask_witness_timeout_seconds(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "subtask_witness_timeout_seconds = 180\n"
        ),
    )
    cfg = load_config(tmp_path)
    assert cfg.subtask_witness_timeout_seconds == 180


def test_load_config_wires_fixup_planner_timeout_s(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\nrepo_path = "{repo}"\ndag_path = "{dag}"\nfixup_planner_timeout_s = 2400\n'
        ),
    )
    cfg = load_config(tmp_path)
    assert cfg.fixup_planner_timeout_s == 2400


def test_load_config_wires_fixup_planner_retries_on_transient(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "fixup_planner_retries_on_transient = 4\n"
        ),
    )
    cfg = load_config(tmp_path)
    assert cfg.fixup_planner_retries_on_transient == 4


def test_load_config_wires_pre_pr_architecture_model(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            'pre_pr_architecture_model = "GLM-5.1-zai"\n'
        ),
    )
    cfg = load_config(tmp_path)
    assert cfg.pre_pr_architecture_model == "GLM-5.1-zai"


def test_orphan_audit_silent_when_override_takes_effect(tmp_path, caplog):
    """A wired field whose override differs from the default must NOT
    fire the orphan-audit warning."""
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "subtask_same_signature_block_count = 10\n"
        ),
    )
    caplog.set_level(logging.WARNING, logger="quikode.config_loader")
    load_config(tmp_path)
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.WARNING
    ]
    assert not any("subtask_same_signature_block_count" in line for line in warnings)


def test_orphan_audit_silent_when_toml_matches_default(tmp_path, caplog):
    """Setting a key to its exact default value is not an orphan; no
    warning should fire because there's nothing to swallow."""
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "max_parallel = 3\n"  # equals Field default
        ),
    )
    caplog.set_level(logging.WARNING, logger="quikode.config_loader")
    load_config(tmp_path)
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.WARNING
    ]
    assert not any("max_parallel" in line for line in warnings)


def test_orphan_audit_silent_for_non_config_keys(tmp_path, caplog):
    """A toml key that isn't a Config field name (typo, sub-table) must
    NOT fire the orphan warning."""
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\nrepo_path = "{repo}"\ndag_path = "{dag}"\n[stacking]\nmax_depth = 12\n'
        ),
    )
    caplog.set_level(logging.WARNING, logger="quikode.config_loader")
    load_config(tmp_path)
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.WARNING
    ]
    # No top-level orphan from the [stacking] sub-table.
    assert warnings == []


def test_orphan_audit_fires_when_loader_silently_swallows_override(tmp_path, caplog):
    """If a Config field is set in toml but the constructed cfg still
    matches the default, the audit MUST emit a WARNING. Exercise the
    helper directly with a synthesized orphan condition (raw says 99,
    cfg shows default 5) that mirrors the bug plan 50 closes."""
    raw: dict[str, object] = {"subtask_same_signature_block_count": 99}
    cfg = ConfigCls(repo_path=tmp_path, dag_path=tmp_path)
    defaults = ConfigCls(repo_path=tmp_path, dag_path=tmp_path)
    assert cfg.subtask_same_signature_block_count == defaults.subtask_same_signature_block_count
    caplog.set_level(logging.WARNING, logger="quikode.config_loader")
    loader_mod._warn_orphan_overrides(raw, cfg, defaults)
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "quikode.config_loader" and rec.levelno == logging.WARNING
    ]
    matches = [w for w in warnings if "subtask_same_signature_block_count" in w]
    assert len(matches) == 1
    assert "99" in matches[0]
    assert "orphan" in matches[0]


def test_load_config_reads_pre_pr_budget_and_release_valve_knobs(tmp_path):
    _scaffold_workspace(
        tmp_path,
        toml_body=(
            'profile = "tanren"\n'
            'repo_path = "{repo}"\n'
            'dag_path = "{dag}"\n'
            "pre_pr_standards_max_medium_findings = 2\n"
            "pre_pr_standards_max_high_findings = 1\n"
            "pre_pr_standards_max_critical_findings = 0\n"
            "pre_pr_architecture_max_medium_findings = 3\n"
            "pre_pr_architecture_max_high_findings = 1\n"
            "pre_pr_architecture_max_critical_findings = 0\n"
            "pre_pr_audit_output_retries = 4\n"
            "pre_pr_release_valve_after_cycles = 6\n"
            'pre_pr_release_valve_defer_stages = ["standards", "architecture"]\n'
            "pre_pr_release_valve_max_critical_findings = 0\n"
        ),
    )

    cfg = load_config(tmp_path)

    assert cfg.pre_pr_standards_max_medium_findings == 2
    assert cfg.pre_pr_standards_max_high_findings == 1
    assert cfg.pre_pr_architecture_max_medium_findings == 3
    assert cfg.pre_pr_audit_output_retries == 4
    assert cfg.pre_pr_release_valve_after_cycles == 6
    assert cfg.pre_pr_release_valve_defer_stages == ["standards", "architecture"]
