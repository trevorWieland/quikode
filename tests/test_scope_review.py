"""Unit tests for `quikode.scope_review`.

The scope reviewer is an advisory layer between the doer's actual diff
and the planner's declared lane. Tests cover:

- Subset short-circuit (no agent call when actually_touched ⊆ declared)
- Legitimate drift (agent says LEGIT → result reflects actual)
- Overreach (agent says NOT LEGIT → result keeps declared)
- Default-LEGIT on agent rc != 0 (don't block on reviewer infra issues)
- Default-LEGIT on unparseable agent output
- JSON envelope parsing across fenced + bare blocks
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode import scope_review
from quikode.config import Config
from quikode.subtask_schema import Subtask
from quikode.types import AgentResult


def _stub_subtask() -> Subtask:
    return Subtask(
        id="S-09-web",
        title="web routes",
        depends_on=(),
        files_to_touch=("apps/web/src/page.tsx", "apps/web/src/i18n/paraglide/messages.ts"),
        boundary="web only",
        acceptance=("typecheck passes",),
        notes="",
    )


def _cfg(tmp_path: Path) -> Config:
    return Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")


def _agent_result(stdout: str, rc: int = 0) -> AgentResult:
    return AgentResult(rc=rc, stdout=stdout, stderr="", duration_s=1.0)


def test_subset_short_circuits_no_agent_call(tmp_path):
    """Actual ⊆ declared → no agent call; pure subset is trivially legit."""
    cfg = _cfg(tmp_path)
    sub = _stub_subtask()
    handle = MagicMock()
    with patch("quikode.scope_review.build_agent") as mock_build:
        result = scope_review.review_scope_drift(
            cfg=cfg,
            handle=handle,
            subtask=sub,
            declared=list(sub.files_to_touch),
            actually_touched=["apps/web/src/page.tsx"],
        )
    assert result.legitimate is True
    assert "subset" in result.reason
    assert mock_build.call_count == 0  # no agent call


def test_legit_drift_keeps_actual(tmp_path):
    """Agent says LEGIT → accepted_files reflects the actual touched set."""
    cfg = _cfg(tmp_path)
    sub = _stub_subtask()
    actual = ["apps/web/src/page.tsx", "apps/web/src/i18n/paraglide/messages.js"]
    fenced = (
        '```json\n{"legitimate": true, "reason": "auto-gen path swap", '
        '"accepted_files": ["apps/web/src/page.tsx", "apps/web/src/i18n/paraglide/messages.js"]}\n```'
    )
    fake_agent = MagicMock()
    fake_agent.run.return_value = _agent_result(fenced)
    with (
        patch("quikode.scope_review.build_agent", return_value=fake_agent),
        patch("quikode.scope_review.prompts_mod.render", return_value="prompt"),
    ):
        result = scope_review.review_scope_drift(
            cfg=cfg,
            handle=MagicMock(),
            subtask=sub,
            declared=list(sub.files_to_touch),
            actually_touched=actual,
        )
    assert result.legitimate is True
    assert "auto-gen" in result.reason
    assert "messages.js" in result.accepted_files[1]


def test_overreach_keeps_declared(tmp_path):
    """Agent says OVERREACH → accepted_files falls back to the declared list."""
    cfg = _cfg(tmp_path)
    sub = _stub_subtask()
    actual = ["apps/web/src/page.tsx", "unrelated/wrong.py"]
    fenced = (
        '```json\n{"legitimate": false, "reason": "touched unrelated/wrong.py", '
        '"accepted_files": ["apps/web/src/page.tsx"]}\n```'
    )
    fake_agent = MagicMock()
    fake_agent.run.return_value = _agent_result(fenced)
    with (
        patch("quikode.scope_review.build_agent", return_value=fake_agent),
        patch("quikode.scope_review.prompts_mod.render", return_value="prompt"),
    ):
        result = scope_review.review_scope_drift(
            cfg=cfg,
            handle=MagicMock(),
            subtask=sub,
            declared=list(sub.files_to_touch),
            actually_touched=actual,
        )
    assert result.legitimate is False
    assert "unrelated" in result.reason
    # Overreach → fall back to declared lane (NOT the agent's accepted_files,
    # since we don't trust an overreach verdict to define the new lane).
    assert result.accepted_files == list(sub.files_to_touch)


def test_agent_rc_nonzero_defaults_legit(tmp_path):
    """Agent infra failure → default LEGITIMATE so reviewer outages don't
    block commits. The audit pipeline still catches genuine quality issues."""
    cfg = _cfg(tmp_path)
    sub = _stub_subtask()
    fake_agent = MagicMock()
    fake_agent.run.return_value = _agent_result("", rc=137)  # OOM-killed
    with (
        patch("quikode.scope_review.build_agent", return_value=fake_agent),
        patch("quikode.scope_review.prompts_mod.render", return_value="prompt"),
    ):
        result = scope_review.review_scope_drift(
            cfg=cfg,
            handle=MagicMock(),
            subtask=sub,
            declared=list(sub.files_to_touch),
            actually_touched=["apps/web/src/page.tsx", "extra.py"],
        )
    assert result.legitimate is True
    assert "rc=137" in result.reason


def test_unparseable_output_defaults_legit(tmp_path):
    """Agent stdout not JSON → default LEGITIMATE."""
    cfg = _cfg(tmp_path)
    sub = _stub_subtask()
    fake_agent = MagicMock()
    fake_agent.run.return_value = _agent_result("not json at all, just prose")
    with (
        patch("quikode.scope_review.build_agent", return_value=fake_agent),
        patch("quikode.scope_review.prompts_mod.render", return_value="prompt"),
    ):
        result = scope_review.review_scope_drift(
            cfg=cfg,
            handle=MagicMock(),
            subtask=sub,
            declared=list(sub.files_to_touch),
            actually_touched=["apps/web/src/page.tsx", "extra.py"],
        )
    assert result.legitimate is True
    assert "unparseable" in result.reason


def test_parse_envelope_handles_bare_json():
    """Agent output without ```json fences still parses if there's a bare object."""
    raw = 'Sure, here you go.\n{"legitimate": true, "reason": "ok", "accepted_files": ["a"]}'
    parsed = scope_review._parse_envelope(raw)
    assert parsed is not None
    assert parsed["legitimate"] is True


def test_parse_envelope_handles_fenced():
    raw = '```json\n{"legitimate": false, "reason": "x", "accepted_files": []}\n```'
    parsed = scope_review._parse_envelope(raw)
    assert parsed is not None
    assert parsed["legitimate"] is False


def test_parse_envelope_garbage_returns_none():
    assert scope_review._parse_envelope("") is None
    assert scope_review._parse_envelope("just prose, no braces") is None
