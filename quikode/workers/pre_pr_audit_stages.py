"""Pre-PR audit stage execution helpers."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime, runtime_shutdown


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class PrePrAuditStageMixin:
    def _resumable_pre_pr_audit_summary(self: Any) -> dict[str, Any] | None:
        """Return a persisted in-progress audit summary that can be resumed."""
        summary = self.store.get_pre_pr_audit_summary(self.node.id)
        if not isinstance(summary, dict):
            return None
        try:
            cycle = int(summary.get("cycle"))
        except (TypeError, ValueError):
            return None
        if cycle < 1 or cycle > int(self.cfg.pre_pr_audit_max_cycles):
            return None
        stages = list(summary.get("stages") or [])
        if not any(s.get("passed") is True for s in stages if isinstance(s, dict)):
            return None
        return summary

    @staticmethod
    def _resumable_pre_pr_stage_names(summary: dict[str, Any]) -> list[str]:
        return [
            str(s.get("name"))
            for s in list(summary.get("stages") or [])
            if isinstance(s, dict) and s.get("passed") is True and s.get("name")
        ]

    @staticmethod
    def _pre_pr_stage_passed(summary: dict[str, Any] | None, stage_name: str) -> bool:
        if not summary:
            return False
        return stage_name in PrePrAuditStageMixin._resumable_pre_pr_stage_names(summary)

    @staticmethod
    def _stage_outcome_from_summary(summary: dict[str, Any] | None, stage_name: str) -> Any | None:
        if not summary:
            return None
        for stage in list(summary.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            if stage.get("name") == stage_name and stage.get("passed") is True:
                return _tw.pre_pr_audit.StageOutcome(
                    name=stage_name,
                    passed=True,
                    summary=str(stage.get("summary") or "resumed from prior passed stage"),
                )
        return None

    def _execute_audit_stages(
        self: Any,
        *,
        cycle: int,
        diff_excerpt: str,
        plan_text: str,
        merge_node_mode: bool,
        resume_summary: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Run audit stages in order, reusing completed persisted passes."""

        def rec(name: str, oc: Any) -> None:
            self.store.update_pre_pr_audit_stage(
                self.node.id,
                cycle=cycle,
                stage_name=name,
                passed=oc.passed,
                summary=oc.summary,
            )

        def enter(label: str) -> None:
            fsm_runtime.enter_pre_pr_auditing(self.store, self.node.id, note=f"pre-pr cycle {cycle}: {label}")

        def run_or_reuse(stage_name: str, runner: Any | None = None, **kwargs: Any) -> Any:
            reused = self._stage_outcome_from_summary(resume_summary, stage_name)
            if reused is not None:
                _tw.log.info(
                    "task %s pre-pr cycle %d stage `%s`: reusing prior PASS after restart",
                    self.node.id,
                    cycle,
                    stage_name,
                )
                return reused
            if runner is None:
                raise RuntimeError(f"no runner configured for pre-pr stage {stage_name}")
            outcome = runner(**kwargs)
            if runtime_shutdown.stop_requested():
                raise runtime_shutdown.ShutdownRequested(
                    f"shutdown requested after pre-pr stage {stage_name}; discarding result"
                )
            rec(stage_name, outcome)
            return outcome

        local_ci = run_or_reuse(
            "local_ci",
            _tw.pre_pr_audit.run_local_ci_gate,
            cfg=self.cfg,
            handle=self._h,
            log_path=self.log_path,
        )
        stages: list[Any] = [local_ci]
        if (not merge_node_mode) or self._merge_integration_subtasks_present():
            contract = self._evaluation_contract()
            cited_standards, cited_architecture = self._collect_cited_refs()
            enter("rubric audit")
            rubric = run_or_reuse(
                "rubric",
                _tw.pre_pr_audit.run_rubric_audit,
                cfg=self.cfg,
                handle=self._h,
                diff_excerpt=diff_excerpt,
                plan_text=plan_text,
                log_path=self.log_path,
            )
            enter("standards audit")
            standards = run_or_reuse(
                "standards",
                _tw.pre_pr_audit.run_standards_audit,
                cfg=self.cfg,
                handle=self._h,
                contract=contract,
                diff_excerpt=diff_excerpt,
                cited_refs=cited_standards,
                log_path=self.log_path,
            )
            enter("architecture audit")
            architecture = run_or_reuse(
                "architecture",
                _tw.pre_pr_audit.run_architecture_audit,
                cfg=self.cfg,
                handle=self._h,
                contract=contract,
                diff_excerpt=diff_excerpt,
                cited_refs=cited_architecture,
                log_path=self.log_path,
            )
            stages.extend([rubric, standards, architecture])
        enter("behavior audit")
        behavior = run_or_reuse(
            "behavior",
            _tw.pre_pr_audit.run_behavior_audit,
            cfg=self.cfg,
            handle=self._h,
            expected_evidence=self._behavior_audit_expected_evidence(merge_node_mode=merge_node_mode),
            diff_excerpt=diff_excerpt,
            plan_text=plan_text,
            log_path=self.log_path,
        )
        stages.append(behavior)
        return stages

    def _collect_cited_refs(self: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return deduped `(standards, architecture)` citations from the plan."""
        cited_standards: list[tuple[str, str]] = []
        cited_architecture: list[tuple[str, str]] = []
        seen_s: set[tuple[str, str]] = set()
        seen_a: set[tuple[str, str]] = set()
        if self.plan is None:
            return cited_standards, cited_architecture
        for subtask in self.plan.subtasks:
            for ref in subtask.standards_referenced:
                key = (ref.doc_path, ref.section)
                if key in seen_s:
                    continue
                seen_s.add(key)
                cited_standards.append(key)
            for ref in subtask.architecture_referenced:
                key = (ref.doc_path, ref.section)
                if key in seen_a:
                    continue
                seen_a.add(key)
                cited_architecture.append(key)
        return cited_standards, cited_architecture

    def _merge_integration_subtasks_present(self: Any) -> bool:
        """True iff any merge-node subtask carries `kind='merge-integration'`."""
        return any(
            (r.get("kind") or "") == "merge-integration" for r in self.store.list_subtasks(self.node.id)
        )

    def _behavior_audit_expected_evidence(self: Any, *, merge_node_mode: bool) -> list[dict]:
        """Spec task evidence, or deduped source-parent evidence for merge nodes."""
        if not merge_node_mode:
            return list(self.node.expected_evidence or [])
        seen: list[dict] = []
        keys: set[str] = set()
        for pid in self.store.get_parent_task_ids(self.node.id):
            pnode = self.dag.nodes.get(pid)
            if pnode is None:
                continue
            for ev in pnode.expected_evidence or ():
                k = _tw.json.dumps(ev, sort_keys=True) if isinstance(ev, dict) else str(ev)
                if k in keys:
                    continue
                keys.add(k)
                seen.append(dict(ev) if isinstance(ev, dict) else ev)
        return seen
