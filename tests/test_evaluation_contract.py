"""Plan 33: tests for the EvaluationContract abstraction + Jinja partial.

Coverage:

- Constructor (`build_for`) produces a stable contract for a synthetic
  `(node, cfg)` pair.
- Persistence round-trip (`persist` → `load`) preserves all four
  StageRubric instances exactly.
- Standards-text capping at 60k chars, truncate-with-marker behavior.
- Jinja partial macros render the contract into all three variants
  (full / single stage card / targeted-by-subtask) with the expected
  tokens present.
"""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import (
    EvaluationContract,
    StageRubric,
    _gather_standards_text,
    build_for,
)
from quikode.prompts import render
from quikode.subtask_schema import RubricTarget, StandardsRef, Subtask


def _build_node(node_id: str = "R-001") -> Node:
    return Node(
        id=node_id,
        kind="behavior",
        milestone="M-1",
        title="Sign-in flow",
        scope="Implement sign-in across web and api",
        depends_on=(),
        completes_behaviors=("B-100",),
        supports_behaviors=(),
        boundary_with_neighbors="auth/, not billing/",
        expected_evidence=(
            {
                "kind": "test",
                "behavior_id": "B-100",
                "interfaces": ["web", "api"],
                "witnesses": ["positive", "falsification"],
                "description": "GET /auth round-trips a session.",
            },
        ),
        playbook=("api: POST /sessions",),
        rationale="first auth slice",
        risks=("password storage policy",),
        raw={},
    )


def _cfg(tmp_path: Path) -> Config:
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        prompts_dir=tmp_path / "prompts-missing",  # falls back to bundled
        worktree_root=tmp_path / ".quikode" / "worktrees",
        log_dir=tmp_path / ".quikode" / "logs",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        pre_pr_rubric_categories=["security", "maintainability", "test_coverage"],
        pre_pr_rubric_min_score=7,
        pre_pr_standards_profile_globs=[],
    )


# ----- build_for -----


def test_build_for_local_ci_carries_command(tmp_path):
    cfg = _cfg(tmp_path)
    node = _build_node()
    contract = build_for(node, cfg)
    assert contract.task_id == "R-001"
    assert contract.local_ci.threshold == "rc=0"
    assert cfg.local_ci_command in contract.local_ci.grading_template


def test_build_for_rubric_renders_all_categories(tmp_path):
    cfg = _cfg(tmp_path)
    node = _build_node()
    contract = build_for(node, cfg)
    for cat in cfg.pre_pr_rubric_categories:
        assert cat in contract.rubric.source_text
    assert str(cfg.pre_pr_rubric_min_score) in contract.rubric.threshold


def test_build_for_behavior_renders_evidence_ids(tmp_path):
    cfg = _cfg(tmp_path)
    node = _build_node()
    contract = build_for(node, cfg)
    # The canonical id for the single evidence row combines behavior_id +
    # kind + witnesses joined with hyphens.
    assert "B-100-test-positive-falsification" in contract.behavior.source_text


def test_build_for_with_empty_rubric_does_not_crash(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"pre_pr_rubric_categories": []})
    node = _build_node()
    contract = build_for(node, cfg)
    # Degenerate-but-valid: source_text has the explanatory placeholder.
    assert "no rubric categories" in contract.rubric.source_text


# ----- persistence round-trip -----


def test_persist_round_trip_preserves_all_stages(tmp_path):
    cfg = _cfg(tmp_path)
    node = _build_node()
    contract = build_for(node, cfg)

    written = contract.persist(cfg.state_dir, node.id)
    assert written.exists()
    raw = json.loads(written.read_text())
    assert raw["task_id"] == "R-001"
    for stage_name in ("local_ci", "rubric", "standards", "behavior"):
        assert stage_name in raw

    loaded = EvaluationContract.load(cfg.state_dir, node.id)
    assert loaded == contract


def test_load_raises_when_artifact_missing(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        EvaluationContract.load(cfg.state_dir, "no-such-task")
    except FileNotFoundError as e:
        assert "evaluation contract not found" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


# ----- standards-text capping -----


def test_gather_standards_text_truncates_at_60k(tmp_path):
    """Exceeding the 60k cap truncates at a line boundary and appends a marker."""
    docs_dir = tmp_path / "docs" / "standards"
    docs_dir.mkdir(parents=True)
    # Write a single doc that goes well over the 60k cap.
    big = docs_dir / "big.md"
    line = "this is line content padding the standards doc.\n"
    # 61k * 50 chars/line ≈ 3M chars — well over the cap.
    big.write_text(line * 60_000)
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"pre_pr_standards_profile_globs": ["docs/standards/*.md"]})
    body, truncated = _gather_standards_text(cfg)
    assert truncated is True
    assert "[STANDARDS DOC TRUNCATED" in body
    assert len(body) < 65_000  # cap + small marker


def test_gather_standards_text_under_cap_no_truncation(tmp_path):
    docs_dir = tmp_path / "docs" / "standards"
    docs_dir.mkdir(parents=True)
    (docs_dir / "small.md").write_text("# small standards\n\nbody.\n")
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"pre_pr_standards_profile_globs": ["docs/standards/*.md"]})
    body, truncated = _gather_standards_text(cfg)
    assert truncated is False
    assert "TRUNCATED" not in body
    assert "small standards" in body


def test_gather_standards_text_with_no_matching_files(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"pre_pr_standards_profile_globs": ["docs/nonexistent/*.md"]})
    body, truncated = _gather_standards_text(cfg)
    assert truncated is False
    assert "no standards documents" in body


# ----- Jinja partial render variants -----


def _render_macro_call(cfg: Config, body: str) -> str:
    """Render an ad-hoc template that imports the macros from the partial.

    The Jinja loader picks up the bundled prompts via Config; the partial
    is shipped in `prompts/_evaluation_context.md.j2`. We pass the body
    in as a child template via `cfg.prompts_dir` after writing it locally.
    """
    template = '{% from "_evaluation_context.md.j2" import ec_full, ec_stage_card, ec_targeted %}\n' + body
    prompts_dir = cfg.prompts_dir
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "_test_macro_caller.md.j2").write_text(template)
    return render(
        cfg,
        "_test_macro_caller.md.j2",
        contract=_make_synthetic_contract(),
        subtask=_make_subtask_with_targets(),
    )


def _make_synthetic_contract() -> EvaluationContract:
    return EvaluationContract(
        task_id="R-001",
        local_ci=StageRubric(
            name="local_ci",
            one_line="local-CI gate",
            threshold="rc=0",
            grading_template="The local-CI grading template body",
            source_text="Command: `just ci`",
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="rubric stage",
            threshold="every category >= 7",
            grading_template="rubric grading template body",
            source_text="- **security**\n- **maintainability**\n",
        ),
        standards=StageRubric(
            name="standards",
            one_line="standards stage",
            threshold="no drift",
            grading_template="standards grading template",
            source_text="standards canonical text...",
        ),
        behavior=StageRubric(
            name="behavior",
            one_line="behavior stage",
            threshold="every witness verified",
            grading_template="behavior grading template",
            source_text="- `B-100-test-positive`: round-trip session",
        ),
    )


def _make_subtask_with_targets() -> Subtask:
    return Subtask(
        id="S-01",
        title="domain types",
        depends_on=(),
        files_to_touch=("foo.rs",),
        boundary="",
        acceptance=("compiles",),
        rubric_targets=(RubricTarget(category="maintainability", predicted_score=8),),
        standards_referenced=(StandardsRef(doc_path="docs/standards/web.md", section="list-views"),),
        behavior_evidence_advanced=("B-100-test-positive",),
    )


def test_partial_ec_full_renders_all_four_stage_cards(tmp_path):
    cfg = _cfg(tmp_path)
    out = _render_macro_call(cfg, "{{ ec_full(contract) }}")
    assert "local_ci stage" in out
    assert "rubric stage" in out
    assert "standards stage" in out
    assert "behavior stage" in out
    assert "rc=0" in out
    assert "every category >= 7" in out


def test_partial_ec_stage_card_renders_one_stage(tmp_path):
    cfg = _cfg(tmp_path)
    out = _render_macro_call(cfg, '{{ ec_stage_card(contract, "rubric") }}')
    assert "rubric stage" in out
    # The other three stage cards should NOT render.
    assert "local_ci stage" not in out
    assert "standards stage" not in out


def test_partial_ec_targeted_filters_to_subtask_targets(tmp_path):
    cfg = _cfg(tmp_path)
    out = _render_macro_call(cfg, "{{ ec_targeted(contract, subtask) }}")
    # Targeted contract always shows local_ci.
    assert "local_ci stage" in out
    # The subtask claims maintainability + B-100-test-positive + web.md.
    assert "maintainability" in out
    assert "predicted score: 8" in out
    assert "B-100-test-positive" in out
    assert "docs/standards/web.md" in out
    assert "list-views" in out


def test_partial_ec_targeted_shows_empty_message_when_no_targets(tmp_path):
    cfg = _cfg(tmp_path)
    bare_subtask = Subtask(
        id="S-Z",
        title="empty",
        depends_on=(),
        files_to_touch=(),
        boundary="",
        acceptance=("ok",),
    )
    template = (
        '{% from "_evaluation_context.md.j2" import ec_targeted %}\n{{ ec_targeted(contract, subtask) }}'
    )
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "_targeted_test.md.j2").write_text(template)
    out = render(
        cfg,
        "_targeted_test.md.j2",
        contract=_make_synthetic_contract(),
        subtask=bare_subtask,
    )
    # local_ci card is always-on.
    assert "local_ci stage" in out
    # Empty-target placeholder text appears for each empty stage.
    assert "no rubric targets claimed" in out
    assert "no standards refs pinned" in out
    assert "no behavior evidence claimed" in out
