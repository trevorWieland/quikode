"""Pre-PR audit cycle report helpers.

Pulled out of `pre_pr.py` so that module stays under the 600-line
architecture budget. Two narrow predicates over a `PipelineCycleResult`:

* `release_valve_report` — when the release valve may open a PR with
  deferred quality findings (config-driven; only the configured
  deferable stages, no critical findings, no non-deferable kinds).
* `structural_failure_report` — when an audit stage failed structurally
  (parse / transport / config error). The doer cannot fix these in the
  target repo; the worker BLOCKs instead of planning toxic fixup work.

Both helpers return None when the predicate doesn't apply, mirroring
the pre-extraction control-flow shape so `pre_pr.py` keeps `if X:` /
`return X` branches.
"""

from __future__ import annotations

from typing import Any

from quikode import pre_pr_audit
from quikode.config import Config

DEFERRED_PRE_PR_FINDINGS_ARTIFACT = "pre_pr_deferred_findings"
NON_DEFERABLE_FINDING_KINDS = {
    "config_error",
    "bootstrap_error",
    "infra",
    "parse_failure",
    "render_failure",
    "transport",
}


def release_valve_report(cfg: Config, cycle_result: Any) -> str | None:
    """Return the PR-body/artifact report when the release valve may
    open a PR with deferred quality findings. None means the normal
    fixup loop must continue."""
    after_cycles = int(cfg.pre_pr_release_valve_after_cycles)
    failed = list(cycle_result.failed_stages)
    if after_cycles < 0 or int(cycle_result.cycle) < after_cycles or not failed:
        return None
    failed_names = {s.name for s in failed}
    if "local_ci" in failed_names or "behavior" in failed_names:
        return None
    deferable = set(cfg.pre_pr_release_valve_defer_stages)
    if not failed_names.issubset(deferable):
        return None

    critical_count = 0
    has_non_deferable_finding = False
    for stage in failed:
        if not stage.findings:
            has_non_deferable_finding = True
        for finding in list(stage.findings or []):
            kind = str(finding.get("kind") or "")
            if kind in NON_DEFERABLE_FINDING_KINDS:
                has_non_deferable_finding = True
            if str(finding.get("severity") or "").lower() == "critical":
                critical_count += 1
    if has_non_deferable_finding or critical_count > int(cfg.pre_pr_release_valve_max_critical_findings):
        return None

    rendered = pre_pr_audit.merge_failed_stage_reports(failed)
    return (
        "## Deferred pre-PR audit findings\n\n"
        f"quikode opened this PR after pre-PR audit cycle {cycle_result.cycle} "
        f"because `local_ci` and `behavior` passed, and only configured "
        "quality-audit content findings remained.\n\n"
        f"Deferred stage(s): {', '.join(sorted(failed_names))}.\n\n"
        "---\n\n"
        f"{rendered}"
    )


def structural_failure_report(cycle_result: Any) -> str | None:
    """Return a block report when an audit stage failed structurally.

    These are quikode/runtime failures, not code-review findings.
    Planning fixup subtasks for them creates toxic work: the doer
    cannot fix an auditor parse failure or transport/config failure
    in the target repo.
    """
    failed = list(cycle_result.failed_stages)
    structural: list[str] = []
    for stage in failed:
        for finding in list(stage.findings or []):
            kind = str(finding.get("kind") or "")
            if kind in NON_DEFERABLE_FINDING_KINDS:
                structural.append(f"{stage.name}:{kind}")
    if not structural:
        return None
    return (
        "pre-PR audit stage failed structurally; blocking instead of "
        "planning target-repo fixups: "
        + ", ".join(structural)
        + "\n\n"
        + pre_pr_audit.merge_failed_stage_reports(failed)
    )


__all__ = [
    "DEFERRED_PRE_PR_FINDINGS_ARTIFACT",
    "NON_DEFERABLE_FINDING_KINDS",
    "release_valve_report",
    "structural_failure_report",
]
