"""Pre Pr worker mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import State, SubtaskState
from quikode.subtask_schema import FixupPlan
from quikode.workers.fixup_coverage import (
    build_coverage_gap_addendum,
    missing_finding_coverage,
    run_fixup_planner_loop,
    split_subtask_rows_for_planner,
)
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class PrePrWorkerMixin:
    def _run_fixup_round(
        self: Any,
        *,
        kind: str,
        round_no: int,
        trigger: str,
        checker_output: str | None = None,
        ci_excerpt: str | None = None,
        review_threads_block: str | None = None,
        triage_root_cause: str | None = None,
        expected_finding_ids: list[str] | None = None,
    ) -> WorkerOutcome | None:
        """Plan a fixup round, append the slices to the subtasks table, and
        run them through the same per-subtask machinery as spec subtasks.

        On planner failure (parse error, empty output) falls back to the
        monolithic `_do(attempt=...)` so we never get stuck without
        ANY attempt at fixing the failure.

        Returns:
            None if all fixup subtasks settled (caller re-checks).
            WorkerOutcome(BLOCKED) if a fixup subtask blocked or the
                fixup planner failed AND the fallback also can't
                make progress (which the caller surfaces as task BLOCKED).
        """
        if fsm_runtime.current_state(self.store, self.node.id) is not State.ADDRESSING_FEEDBACK:
            fsm_runtime.enter_fixup_planning(
                self.store,
                self.node.id,
                note=f"{kind} round {round_no} ({trigger})",
            )
        fixup_plan = self._invoke_fixup_planner(
            kind=kind,
            round_no=round_no,
            trigger=trigger,
            checker_output=checker_output,
            ci_excerpt=ci_excerpt,
            review_threads_block=review_threads_block,
            triage_root_cause=triage_root_cause,
            audit_findings=expected_finding_ids,
        )
        # Completeness check (Plan 33 PR-B): for audit-driven fixup rounds,
        # every `expected_finding_ids` entry must be covered. The driver-
        # side wrapper unions `rubric_targets` / `standards_referenced` /
        # `behavior_evidence_advanced` plus the plan-level
        # `findings_addressed` array, tolerating namespace prefixes; see
        # `fixup_coverage.missing_finding_coverage`.
        if (
            fixup_plan is not None
            and fixup_plan.subtasks
            and expected_finding_ids
            and kind == "fixup-pre-pr-audit"
        ):
            missing = self._missing_finding_coverage(fixup_plan, expected_finding_ids)
            if missing:
                _tw.log.warning(
                    "fixup planner missed %d finding(s) for %s round %d; re-prompting: %s",
                    len(missing),
                    kind,
                    round_no,
                    ", ".join(sorted(missing)[:8]),
                )
                augmented_root = build_coverage_gap_addendum(missing, triage_root_cause)
                fixup_plan = self._invoke_fixup_planner(
                    kind=kind,
                    round_no=round_no,
                    trigger=trigger,
                    checker_output=checker_output,
                    ci_excerpt=ci_excerpt,
                    review_threads_block=review_threads_block,
                    triage_root_cause=augmented_root,
                    audit_findings=expected_finding_ids,
                )
                if fixup_plan is not None and fixup_plan.subtasks:
                    still_missing = self._missing_finding_coverage(fixup_plan, expected_finding_ids)
                    if still_missing:
                        note = (
                            f"fixup planner still missed {len(still_missing)} finding(s) "
                            f"after re-prompt for {kind} round {round_no}: "
                            f"{', '.join(sorted(still_missing)[:8])}; BLOCKing"
                        )
                        _tw.log.warning(note)
                        fsm_runtime.block_current(
                            self.store,
                            self.node.id,
                            note=note,
                            last_error=note[:1000],
                        )
                        return WorkerOutcome(State.BLOCKED, note)
        if fixup_plan is None or not fixup_plan.subtasks:
            note = (
                f"fixup planner returned empty/invalid plan for {kind} round "
                f"{round_no} ({trigger}); BLOCKing for operator review"
            )
            _tw.log.warning(note)
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=note,
                last_error=note[:1000],
            )
            return WorkerOutcome(State.BLOCKED, note)

        # Persist the new fixup subtasks (additive — does NOT delete spec rows).
        self.store.append_subtasks(
            self.node.id,
            [
                {
                    "subtask_id": s.id,
                    "title": s.title,
                    "depends_on": list(s.depends_on),
                    "files_to_touch": list(s.files_to_touch),
                    "boundary": s.boundary,
                    "acceptance": list(s.acceptance),
                    "notes": s.notes,
                    "kind": s.kind or kind,
                }
                for s in fixup_plan.subtasks
            ],
        )
        _tw.log.info(
            "fixup round %d (%s): planned %d subtask(s): %s",
            round_no,
            kind,
            len(fixup_plan.subtasks),
            ", ".join(s.id for s in fixup_plan.subtasks),
        )
        return self._run_subtask_set(list(fixup_plan.subtasks))

    @staticmethod
    def _missing_finding_coverage(plan: FixupPlan, expected_finding_ids: list[str]) -> set[str]:
        """Plan 33 PR-B: stage-typed audit-completeness check.

        Implementation lives in `quikode.workers.fixup_coverage` so this
        module stays under the architecture line-budget; this thin
        wrapper preserves the existing call site shape."""
        return missing_finding_coverage(plan, expected_finding_ids)

    def _invoke_fixup_planner(
        self: Any,
        *,
        kind: str,
        round_no: int,
        trigger: str,
        checker_output: str | None,
        ci_excerpt: str | None,
        review_threads_block: str | None,
        triage_root_cause: str | None,
        audit_findings: list[str] | None = None,
    ) -> FixupPlan | None:
        """Run the fixup planner agent (JsonAgent layer) + validate.

        Plan 33 calibration: the fixup driver runs `validate_finding_coverage`
        (replaces rubric_coverage), `validate_evidence_partition`, and
        `validate_standards_refs` / `validate_architecture_refs`. Plan 38
        PR-B.4: the agent runs through `make_agent("fixup_planner", cfg)`
        and the retry loop lives in `fixup_coverage.run_fixup_planner_loop`
        (extracted to keep this module under the 600-line architecture
        budget). This method just builds the prompt + delegates.
        """
        done_subtasks, prior_fixup_subtasks = split_subtask_rows_for_planner(
            self.store.list_subtasks(self.node.id), SubtaskState.DONE.value
        )
        original_final_acceptance: list[str] = []
        if self.plan is not None:
            original_final_acceptance = list(self.plan.final_acceptance)
        contract = self._evaluation_contract()
        prompt = _tw.prompts.fixup_planner_prompt(
            self.cfg,
            self.node,
            contract,
            kind=kind,
            round_no=round_no,
            max_rounds=self.cfg.fixup_max_rounds,
            trigger=trigger,
            original_final_acceptance=original_final_acceptance,
            done_subtasks=done_subtasks,
            prior_fixup_subtasks=prior_fixup_subtasks,
            checker_output=checker_output,
            ci_excerpt=ci_excerpt,
            review_threads_block=review_threads_block,
            triage_root_cause=triage_root_cause,
        )
        self._write_log_header(f"FIXUP PLANNER {kind} round {round_no}", prompt)
        return run_fixup_planner_loop(
            self,
            kind=kind,
            round_no=round_no,
            base_prompt=prompt,
            audit_findings=audit_findings,
            contract=contract,
            log=_tw.log,
        )

    def _run_manual_probes(self: Any) -> str:
        """Run `kind="manual"` items from `expected_evidence` and return
        a rendered block for the checker prompt. Never raises — any
        runner failure degrades to "no manual probes ran"."""
        try:
            probes = _tw.manual_probe.collect_probes_from_evidence(list(self.node.expected_evidence))
        except Exception as e:
            _tw.log.warning("manual probes: collect raised %s; skipping", e)
            return ""
        if not probes:
            return ""
        if self.handle is None:
            _tw.log.info("manual probes: no container handle; skipping %d probe(s)", len(probes))
            return ""
        _tw.log.info("manual probes: running %d probe(s) for task %s", len(probes), self.node.id)
        try:
            with _tw.manual_probe.ManualProbeRunner(
                handle=self.handle,
                exec_in=_tw.exec_in,
                log_path=self.log_path,
                credentials=_tw.manual_probe.credentials_from_env(["TANREN_MCP_API_KEY", "TANREN_API_KEY"]),
            ) as runner:
                results = runner.run_all_probes(probes)
        except Exception as e:
            _tw.log.warning("manual probes: runner raised %s; degrading", e)
            return ""
        return _tw.manual_probe.render_probe_block(results)

    def _commit_push(self: Any) -> WorkerOutcome | None:
        # v3 stacked-diffs fix: parent merge may have landed during final-check.
        rebase_outcome = self._handle_parent_rebase_if_needed()
        if rebase_outcome:
            return rebase_outcome
        # Per-subtask flow already commit+push'd; fast-forward when the loop
        # left us in PUSHING / PLANNING (full resume) / DOING_SUBTASK (partial).
        if self._fast_forward_to_local_ci_if_subtasks_done():
            return None
        fsm_runtime.enter_committing(self.store, self.node.id)
        msg = f"{self.node.id}: {self.node.title}\n\nPlanned and implemented by quikode."
        rc, out = _tw.github.commit_all(self._h, msg, log_path=self.log_path)
        branch = str(self._row()["branch"])
        if rc != 0:
            if "nothing to commit" in out or "no changes added to commit" in out:
                # Working tree is clean. With v3 per-subtask commits, this is
                # the common case: every subtask already committed its slice
                # during the loop. Check if the branch carries those commits
                # ahead of the base; if so, push and continue. Only treat as a
                # genuine no-op when the branch is also empty.
                ahead = _tw.github.ahead_count(
                    self._h, branch, base=self.cfg.base_branch, log_path=self.log_path
                )
                if ahead > 0:
                    _tw.log.info(
                        "no uncommitted diff but branch is %d commits ahead of %s — proceeding to push",
                        ahead,
                        self.cfg.base_branch,
                    )
                    # fall through to push
                else:
                    fsm_runtime.enter_pushing(self.store, self.node.id, note="no diff before final push")
                    fsm_runtime.enter_local_ci_checking(
                        self.store,
                        self.node.id,
                        note="no diff - task already complete or doer made no changes",
                    )
                    _tw.sound.ding()
                    return WorkerOutcome(State.PENDING_CI, "no diff")
            else:
                # commit failed for a real reason (hook gate, repo state) and
                # the per-subtask flow already commits each slice — so a
                # monolithic commit failure here means something the audit
                # gauntlet would flag anyway. Block with the failure output;
                # operator inspects the _tw.worktree.
                raise RuntimeError(f"commit failed (post-subtasks): {out[:1000]}")

        fsm_runtime.enter_pushing(self.store, self.node.id)
        rc, out = _tw.github.push(self._h, branch, remote=self.cfg.pr_remote, log_path=self.log_path)
        if rc != 0:
            raise RuntimeError(f"push failed: {out[:1000]}")
        return None

    def _run_pre_pr_pipeline(self: Any, *, merge_node_mode: bool = False) -> WorkerOutcome | None:
        """5-stage gate before opening a PR. Returns None on pass,
        WorkerOutcome(BLOCKED) after `cfg.pre_pr_audit_max_cycles` fails.
        Each cycle runs all stages, merges failed-stage findings into a
        triage bundle, hands them to the fixup planner
        (`kind="fixup-pre-pr-audit"`), and re-runs from the top.

        Plan 32 PR-B / Plan 35 PR-B: `merge_node_mode=True` adapts for a
        merge-node — `local_ci` + `behavior` always run; `rubric` +
        `standards` + `architecture` are skipped unless the cycle's
        subtasks include `kind="merge-integration"` (the merge-doer
        emitted real new code). The `behavior` audit's
        `expected_evidence` is the union of source parents'
        `expected_evidence`.
        """
        resume_summary = self._resumable_pre_pr_audit_summary()
        start_cycle = int(resume_summary["cycle"]) if resume_summary else 1
        for cycle in range(start_cycle, self.cfg.pre_pr_audit_max_cycles + 1):
            _tw.log.info(
                "task %s: pre-pr pipeline cycle %d/%d", self.node.id, cycle, self.cfg.pre_pr_audit_max_cycles
            )
            cycle_resume_summary = (
                resume_summary if resume_summary and int(resume_summary["cycle"]) == cycle else None
            )
            if cycle_resume_summary:
                passed = ", ".join(self._resumable_pre_pr_stage_names(cycle_resume_summary))
                _tw.log.info(
                    "task %s: resuming pre-pr audit cycle %d after passed stage(s): %s",
                    self.node.id,
                    cycle,
                    passed,
                )
            else:
                # Seed the audit summary so the TUI shows queued / in-flight /
                # done states for each stage as the cycle progresses.
                self.store.begin_pre_pr_audit_cycle(self.node.id, cycle)
            if not self._pre_pr_stage_passed(cycle_resume_summary, "local_ci"):
                fsm_runtime.enter_local_ci_checking(
                    self.store,
                    self.node.id,
                    note=f"pre-pr cycle {cycle}: local-ci ({self.cfg.local_ci_command})",
                )

            # Build the diff excerpt against the base branch — every audit
            # consumes this. Compute once per cycle (commits may have
            # changed during the prior cycle's fixup loop).
            diff_excerpt = self._compute_branch_diff_excerpt()
            plan_text = str(self._row().get("plan_text") or "")

            stages = self._execute_audit_stages(
                cycle=cycle,
                diff_excerpt=diff_excerpt,
                plan_text=plan_text,
                merge_node_mode=merge_node_mode,
                resume_summary=cycle_resume_summary,
            )
            cycle_result = _tw.pre_pr_audit.PipelineCycleResult(cycle=cycle, stages=stages)
            for s in cycle_result.stages:
                _tw.log.info(
                    "task %s pre-pr cycle %d stage `%s`: %s",
                    self.node.id,
                    cycle,
                    s.name,
                    "PASS" if s.passed else "FAIL",
                )

            if cycle_result.passed:
                _tw.log.info(
                    "task %s pre-pr pipeline passed on cycle %d/%d — proceeding to open PR",
                    self.node.id,
                    cycle,
                    self.cfg.pre_pr_audit_max_cycles,
                )
                return None

            # Failure path: merge findings → triage → fixup planner.
            fsm_runtime.enter_fixup_planning(
                self.store,
                self.node.id,
                note=(
                    f"pre-pr cycle {cycle} failed: " + ", ".join(s.name for s in cycle_result.failed_stages)
                ),
            )
            findings_block = _tw.pre_pr_audit.merge_failed_stage_reports(cycle_result.failed_stages)
            expected_finding_ids = _tw.pre_pr_audit.collect_finding_ids(cycle_result.failed_stages)
            self.store.add_artifact(
                self.node.id,
                f"pre_pr_audit:cycle_{cycle}",
                findings_block,
            )
            # Completeness-augmented findings block: prepend an explicit
            # "every id below MUST appear in your `findings_addressed`"
            # instruction so the planner cannot drop findings to fit a
            # smaller subtask count.
            if expected_finding_ids:
                augmented = (
                    "## Required finding coverage\n\n"
                    "Every id below MUST appear in your output's "
                    "`findings_addressed` array AND be addressed by at "
                    "least one subtask's stage-typed coverage "
                    "(`rubric_targets`, `standards_referenced`, "
                    "`architecture_referenced`, or `behavior_evidence_advanced` "
                    "matching the finding's namespace). The per-subtask `addresses_findings` "
                    "field is gone (Plan 33 D2). Dropping ids is forbidden.\n\n"
                    + "\n".join(f"- `{fid}`" for fid in expected_finding_ids)
                    + "\n\n---\n\n"
                    + findings_block
                )
            else:
                augmented = findings_block
            outcome = self._run_fixup_round(
                kind="fixup-pre-pr-audit",
                round_no=cycle,
                trigger="pre_pr_audit",
                triage_root_cause=augmented[:16000],
                expected_finding_ids=expected_finding_ids,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                # Fixup ceiling exhausted on the audit round — surface as
                # a task BLOCK with the merged findings as the operator
                # context. The block-forensics dump (separate path) picks
                # this up automatically since the artifact is on the row.
                return outcome
            # Loop back to the top — re-run the full pipeline against
            # whatever the doer just landed.
            resume_summary = None

        # Exhausted cycles.
        note = (
            f"pre-PR audit pipeline exhausted {self.cfg.pre_pr_audit_max_cycles} "
            "cycle(s) without a clean pass — manual review required"
        )
        fsm_runtime.block_current(
            self.store,
            self.node.id,
            note=note,
            last_error=note[:1000],
        )
        return WorkerOutcome(State.BLOCKED, note)

    def _fast_forward_to_local_ci_if_subtasks_done(self: Any) -> bool:
        cur = fsm_runtime.current_state(self.store, self.node.id)
        if cur not in (State.PUSHING, State.PLANNING, State.DOING_SUBTASK):
            return False
        if cur is State.PLANNING:
            fsm_runtime.enter_doing_subtask(self.store, self.node.id, note="resume: subtasks already DONE")
            cur = State.DOING_SUBTASK
        if cur is State.DOING_SUBTASK:
            fsm_runtime.enter_checking_subtask(self.store, self.node.id, note="fast-forward")
            fsm_runtime.enter_committing(self.store, self.node.id, note="fast-forward")
            fsm_runtime.enter_pushing(self.store, self.node.id, note="fast-forward")
        fsm_runtime.enter_local_ci_checking(
            self.store, self.node.id, note="all subtasks committed and pushed via per-subtask flow"
        )
        return True

    def _compute_branch_diff_excerpt(self: Any, max_lines: int = 1500) -> str:
        """Capture the worktree branch diff against `cfg.base_branch`. Used
        by the audit agents as the canonical "what changed" reference.

        Truncated to `max_lines` so the prompts stay within model context
        windows. The audit prompts further truncate per-stage based on
        which stage is most diff-hungry."""
        rc, out = self._git_in_workspace(["diff", f"{self.cfg.pr_remote}/{self.cfg.base_branch}...HEAD"])
        if rc != 0 or not out:
            return ""
        lines = out.splitlines()
        if len(lines) > max_lines:
            head = lines[:max_lines]
            head.append(f"... (diff truncated; {len(lines) - max_lines} more lines)")
            return "\n".join(head)
        return out

    def _resumable_pre_pr_audit_summary(self: Any) -> dict[str, Any] | None:
        """Return a persisted in-progress audit summary that can be resumed.

        The summary stores short stage status for TUI display. It is safe to
        reuse already-passed stages even when later stages failed; failed
        stages require full structured finding reports, so the worker reruns
        them.
        """
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
        return stage_name in PrePrWorkerMixin._resumable_pre_pr_stage_names(summary)

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
        """Run the audit stages in order. On a merge-node cycle without
        `kind="merge-integration"` subtasks, rubric + standards +
        architecture skip (only local_ci + behavior gate the integration
        commit). Plan 35 PR-B grew the order to FIVE stages:
        `local_ci, rubric, standards, architecture, behavior`."""

        def rec(name: str, oc: Any) -> None:
            self.store.update_pre_pr_audit_stage(
                self.node.id, cycle=cycle, stage_name=name, passed=oc.passed, summary=oc.summary
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
        """Plan 35 PR-B: union the `standards_referenced` and
        `architecture_referenced` citations across all subtasks of this
        task's plan. Returns `(cited_standards, cited_architecture)`
        — each a list of `(doc_path, section)` tuples, deduplicated.
        Empty lists when there's no parsed plan (e.g. resumed merge node
        with no spec planner output).
        """
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
        """True iff any of the merge-node's subtasks carry
        kind='merge-integration'. Plan 32 PR-B: re-enables rubric +
        standards on cycles where the merge-doer emitted real code."""
        return any(
            (r.get("kind") or "") == "merge-integration" for r in self.store.list_subtasks(self.node.id)
        )

    def _behavior_audit_expected_evidence(self: Any, *, merge_node_mode: bool) -> list[dict]:
        """Spec task: `node.expected_evidence`. Merge-node: union of
        every source parent's `expected_evidence` from the DAG, deduped.
        The merge-node has no DAG node — parents come from
        `store.get_parent_task_ids`."""
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
