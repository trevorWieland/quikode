# Plan 41 — quality gate budgets and release valve

## Problem

The standards and architecture audits used a hard rule: any medium-or-higher
finding failed the stage. That made them much less tunable than the rubric
audit, which has a configurable pass threshold and can use a failing cycle to
collect additional cleanup findings.

Long-running pre-PR cycles also had no release valve. A task with green local
CI, green behavior evidence, and one remaining quality finding could burn cycle
after cycle instead of reaching human review with the residual concern attached.

## Change

Standards and architecture audits now gate on configurable severity budgets:

- `pre_pr_standards_max_medium_findings`
- `pre_pr_standards_max_high_findings`
- `pre_pr_standards_max_critical_findings`
- `pre_pr_architecture_max_medium_findings`
- `pre_pr_architecture_max_high_findings`
- `pre_pr_architecture_max_critical_findings`

Defaults allow one medium finding and zero high/critical findings. Low findings
remain advisory. If a severity budget is exceeded, the failed stage forwards all
of its findings, including advisory lows, into the fixup bundle.

The pre-PR release valve is configurable via:

- `pre_pr_release_valve_after_cycles` (`-1` disables)
- `pre_pr_release_valve_defer_stages`
- `pre_pr_release_valve_max_critical_findings`

After the configured cycle count, quikode may open the PR when local CI and
behavior pass and only configured quality stages remain failing. Config errors,
infra failures, transport failures, parse failures, local-CI failures, behavior
failures, and over-budget critical findings stay blocking. Deferred findings are
persisted as a task artifact and included in the generated PR body.

## Validation

- `uv run ruff check quikode/config.py quikode/config_loader.py quikode/config_template.py quikode/evaluation_contract.py quikode/pre_pr_audit.py quikode/pre_pr_audit_standards.py quikode/pre_pr_audit_architecture.py quikode/workers/pre_pr.py quikode/workers/pr_lifecycle.py tests/test_pre_pr_release_valve.py tests/test_pre_pr_audit.py tests/test_pre_pr_architecture_audit.py tests/test_pre_pr_pipeline_five_stage.py tests/test_config_loader_audit_log.py tests/test_config_schema.py`
- `uv run ruff format --check quikode/config.py quikode/config_loader.py quikode/config_template.py quikode/evaluation_contract.py quikode/pre_pr_audit.py quikode/pre_pr_audit_standards.py quikode/pre_pr_audit_architecture.py quikode/workers/pre_pr.py quikode/workers/pr_lifecycle.py tests/test_pre_pr_release_valve.py tests/test_pre_pr_audit.py tests/test_pre_pr_architecture_audit.py tests/test_pre_pr_pipeline_five_stage.py tests/test_config_loader_audit_log.py tests/test_config_schema.py`
- `uv run ty check quikode/config.py quikode/config_loader.py quikode/config_template.py quikode/evaluation_contract.py quikode/pre_pr_audit.py quikode/pre_pr_audit_standards.py quikode/pre_pr_audit_architecture.py quikode/workers/pre_pr.py quikode/workers/pr_lifecycle.py tests/test_pre_pr_release_valve.py tests/test_pre_pr_audit.py tests/test_pre_pr_architecture_audit.py tests/test_pre_pr_pipeline_five_stage.py tests/test_config_loader_audit_log.py tests/test_config_schema.py`
- `uv run pytest tests/test_pre_pr_audit.py tests/test_pre_pr_architecture_audit.py tests/test_pre_pr_pipeline_five_stage.py tests/test_pre_pr_release_valve.py tests/test_config_loader_audit_log.py tests/test_config_schema.py -q`
