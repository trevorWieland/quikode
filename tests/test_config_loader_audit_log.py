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
