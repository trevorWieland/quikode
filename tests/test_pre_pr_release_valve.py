"""Regression tests for the pre-PR release valve / haste mode."""

from __future__ import annotations

from pathlib import Path

from quikode import pre_pr_audit
from quikode.config import Config
from quikode.dag import Node
from quikode.state import Store
from quikode.workers.pr_lifecycle import PrLifecycleWorkerMixin
from quikode.workers.pre_pr import _pre_pr_release_valve_report


def _cfg(tmp_path: Path, **overrides) -> Config:
    return Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", **overrides)


def _cycle(*stages: pre_pr_audit.StageOutcome, cycle: int = 5) -> pre_pr_audit.PipelineCycleResult:
    return pre_pr_audit.PipelineCycleResult(cycle=cycle, stages=list(stages))


def _pass(name: pre_pr_audit.StageName) -> pre_pr_audit.StageOutcome:
    return pre_pr_audit.StageOutcome(name=name, passed=True, summary="ok")


def _fail(
    name: pre_pr_audit.StageName,
    *,
    findings: list[dict] | None = None,
) -> pre_pr_audit.StageOutcome:
    return pre_pr_audit.StageOutcome(
        name=name,
        passed=False,
        summary=f"{name} failed",
        findings=findings or [{"id": f"{name}-finding", "kind": "content"}],
    )


def test_release_valve_defers_quality_content_after_default_cycle_budget(tmp_path):
    cfg = _cfg(tmp_path)
    result = _cycle(
        _pass("local_ci"),
        _fail("rubric", findings=[{"id": "category-security", "kind": "rubric_below_threshold"}]),
        _fail("standards", findings=[{"id": "std-1", "severity": "high"}]),
        _pass("architecture"),
        _pass("behavior"),
    )

    report = _pre_pr_release_valve_report(cfg, result)

    assert report is not None
    assert "Deferred stage(s): rubric, standards." in report
    assert "category-security" in report
    assert "std-1" in report


def test_release_valve_can_be_disabled(tmp_path):
    cfg = _cfg(tmp_path, pre_pr_release_valve_after_cycles=-1)
    result = _cycle(_pass("local_ci"), _fail("standards"), _pass("behavior"))

    assert _pre_pr_release_valve_report(cfg, result) is None


def test_release_valve_waits_until_configured_cycle(tmp_path):
    cfg = _cfg(tmp_path)
    result = _cycle(_pass("local_ci"), _fail("standards"), _pass("behavior"), cycle=4)

    assert _pre_pr_release_valve_report(cfg, result) is None


def test_release_valve_never_defers_local_ci_or_behavior_failures(tmp_path):
    cfg = _cfg(tmp_path)

    local_ci_failed = _cycle(_fail("local_ci"), _fail("standards"), _pass("behavior"))
    behavior_failed = _cycle(_pass("local_ci"), _fail("standards"), _fail("behavior"))

    assert _pre_pr_release_valve_report(cfg, local_ci_failed) is None
    assert _pre_pr_release_valve_report(cfg, behavior_failed) is None


def test_release_valve_rejects_parse_infra_config_and_transport_failures(tmp_path):
    cfg = _cfg(tmp_path)
    for kind in ("parse_failure", "infra", "config_error", "transport"):
        result = _cycle(
            _pass("local_ci"),
            _fail("standards", findings=[{"kind": kind, "message": "not content"}]),
            _pass("behavior"),
        )
        assert _pre_pr_release_valve_report(cfg, result) is None


def test_release_valve_rejects_failed_stage_without_structured_findings(tmp_path):
    cfg = _cfg(tmp_path)
    result = _cycle(
        _pass("local_ci"),
        pre_pr_audit.StageOutcome("standards", passed=False, summary="failed without findings"),
        _pass("behavior"),
    )

    assert _pre_pr_release_valve_report(cfg, result) is None


def test_release_valve_rejects_critical_findings_by_default(tmp_path):
    cfg = _cfg(tmp_path)
    result = _cycle(
        _pass("local_ci"),
        _fail("architecture", findings=[{"id": "arch-critical", "severity": "critical"}]),
        _pass("behavior"),
    )

    assert _pre_pr_release_valve_report(cfg, result) is None


def test_release_valve_respects_configured_deferable_stages(tmp_path):
    cfg = _cfg(tmp_path, pre_pr_release_valve_defer_stages=["standards"])
    result = _cycle(_pass("local_ci"), _fail("rubric"), _pass("behavior"))

    assert _pre_pr_release_valve_report(cfg, result) is None


def test_pr_body_includes_deferred_findings_artifact(tmp_path):
    node = Node(
        id="R-001",
        kind="behavior",
        milestone="M-1",
        title="Release valve task",
        scope="Do the work.",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    store = Store(tmp_path / "q.db")
    store.add_artifact("R-001", "pre_pr_deferred_findings", "## Deferred pre-PR audit findings\n\nstd-1")
    worker = type(
        "Worker",
        (PrLifecycleWorkerMixin,),
        {
            "node": node,
            "store": store,
            "plan_text": "plan text",
        },
    )()

    body = worker._pr_body()

    assert "## Deferred pre-PR audit findings" in body
    assert "std-1" in body
    store.close()
