"""Pre Pr worker mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import State, SubtaskState
from quikode.subtask_schema import FixupPlan, PlanValidationError
from quikode.workers.fixup_coverage import missing_finding_coverage
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
        )
        # Completeness check (Plan 33 PR-B): for audit-driven fixup rounds,
        # every `expected_finding_ids` entry must be covered. PR-A retired
        # `addresses_findings` per-subtask; PR-B replaces the completeness
        # rule with a stage-typed union over `rubric_targets` /
        # `standards_referenced` / `behavior_evidence_advanced` plus the
        # plan-level `findings_addressed` array. We accept either path
        # (the planner declares `findings_addressed` AND advances the
        # corresponding stage-typed field) so the audit-bundle matcher
        # remains tolerant of finding-id namespaces (rubric:..., behavior:...).
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
                gap_addendum = (
                    "## Coverage gap from your previous attempt\n\n"
                    "Your previous plan missed the following finding ids. "
                    "Include each one in `findings_addressed` AND in at least "
                    "one subtask's stage-typed coverage (`rubric_targets`, "
                    "`standards_referenced`, or `behavior_evidence_advanced` "
                    "matching the finding's namespace):\n\n"
                    + "\n".join(f"- `{fid}`" for fid in sorted(missing))
                    + "\n\n"
                    "Re-emit the COMPLETE plan (do not emit only the deltas)."
                )
                augmented_root = (gap_addendum + "\n\n---\n\n" + (triage_root_cause or ""))[:16000]
                fixup_plan = self._invoke_fixup_planner(
                    kind=kind,
                    round_no=round_no,
                    trigger=trigger,
                    checker_output=checker_output,
                    ci_excerpt=ci_excerpt,
                    review_threads_block=review_threads_block,
                    triage_root_cause=augmented_root,
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
    ) -> FixupPlan | None:
        """Run the fixup planner agent and parse its output. Returns None on
        any error so the caller can fall back to the monolithic doer."""
        agent = _tw.build_agent(self.cfg.planner)
        # Build the context the planner needs: original final_acceptance,
        # done spec subtasks, and any prior fixup subtasks (so the new round
        # can avoid duplicating earlier fixup work).
        rows = self.store.list_subtasks(self.node.id)
        done_subtasks: list[dict] = []
        prior_fixup_subtasks: list[dict] = []
        for r in rows:
            kind_val = (r.get("kind") or "spec") if isinstance(r, dict) else "spec"
            row_view = {
                "subtask_id": r["subtask_id"],
                "title": r.get("title") or "",
                "kind": kind_val,
                "state": r["state"],
            }
            if kind_val == "spec":
                if r["state"] == SubtaskState.DONE.value:
                    done_subtasks.append(row_view)
            else:
                prior_fixup_subtasks.append(row_view)
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

        # rc=124 maps to either a real timeout or `agents.base._is_transient_container_failure`
        # (codex CLI flake, container hiccup). Retry once or twice before giving up so
        # infra noise doesn't burn fixup_max_rounds on a transient.
        retries_left = self.cfg.fixup_planner_retries_on_transient
        attempt_no = 0
        while True:
            attempt_no += 1
            result = agent.run(
                prompt,
                handle=self._h,
                log_path=self.log_path,
                timeout=self.cfg.fixup_planner_timeout_s,
            )
            self.store.record_agent_call(
                self.node.id,
                phase=f"fixup_planner:{kind}",
                cli=self.cfg.planner.cli,
                model=self.cfg.planner.model,
                rc=result.rc,
                duration_s=result.duration_s or 0,
                tokens_used=result.tokens_used,
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                tokens_cached_read=result.tokens_cached_read,
                tokens_cached_creation=result.tokens_cached_creation,
                cost_usd=result.cost_usd,
            )
            self.store.add_artifact(
                self.node.id,
                f"fixup_planner_output:{kind}:{round_no}:attempt{attempt_no}",
                result.stdout,
            )
            if result.rc == 0:
                break
            if result.rc == 124 and retries_left > 0:
                retries_left -= 1
                _tw.log.warning(
                    "fixup planner rc=124 (timeout/transient) for %s round %d, retrying (%d left)",
                    kind,
                    round_no,
                    retries_left,
                )
                continue
            _tw.log.warning("fixup planner exited rc=%d (kind=%s round=%d)", result.rc, kind, round_no)
            return None
        try:
            return _tw.parse_fixup_planner_output(result.stdout)
        except PlanValidationError as e:
            _tw.log.warning(
                "fixup planner output didn't validate (kind=%s round=%d): %s",
                kind,
                round_no,
                e,
            )
            return None

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
        """4-stage gate before opening a PR. Returns None on pass,
        WorkerOutcome(BLOCKED) after `cfg.pre_pr_audit_max_cycles` fails.
        Each cycle runs all stages, merges failed-stage findings into a
        triage bundle, hands them to the fixup planner
        (`kind="fixup-pre-pr-audit"`), and re-runs from the top.

        Plan 32 PR-B: `merge_node_mode=True` adapts for a merge-node —
        `local_ci` + `behavior` always run; `rubric` + `standards` are
        skipped unless the cycle's subtasks include `kind="merge-integration"`
        (the merge-doer emitted real new code). The `behavior` audit's
        `expected_evidence` is the union of source parents' `expected_evidence`.
        """
        for cycle in range(1, self.cfg.pre_pr_audit_max_cycles + 1):
            _tw.log.info(
                "task %s: pre-pr pipeline cycle %d/%d", self.node.id, cycle, self.cfg.pre_pr_audit_max_cycles
            )
            # Seed the audit summary so the TUI shows queued / in-flight /
            # done states for each stage as the cycle progresses.
            self.store.begin_pre_pr_audit_cycle(self.node.id, cycle)
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
                    "(`rubric_targets`, `standards_referenced`, or "
                    "`behavior_evidence_advanced` matching the finding's "
                    "namespace). The per-subtask `addresses_findings` "
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

    def _execute_audit_stages(
        self: Any, *, cycle: int, diff_excerpt: str, plan_text: str, merge_node_mode: bool
    ) -> list[Any]:
        """Run the audit stages in order. On a merge-node cycle without
        `kind="merge-integration"` subtasks, rubric + standards skip."""

        def rec(name: str, oc: Any) -> None:
            self.store.update_pre_pr_audit_stage(
                self.node.id, cycle=cycle, stage_name=name, passed=oc.passed, summary=oc.summary
            )

        def enter(label: str) -> None:
            fsm_runtime.enter_pre_pr_auditing(self.store, self.node.id, note=f"pre-pr cycle {cycle}: {label}")

        local_ci = _tw.pre_pr_audit.run_local_ci_gate(cfg=self.cfg, handle=self._h, log_path=self.log_path)
        rec("local_ci", local_ci)
        stages: list[Any] = [local_ci]
        if (not merge_node_mode) or self._merge_integration_subtasks_present():
            standards_text = _tw.pre_pr_audit.collect_standards_text(
                self.cfg, contract=self._evaluation_contract()
            )
            enter("rubric audit")
            rubric = _tw.pre_pr_audit.run_rubric_audit(
                cfg=self.cfg,
                handle=self._h,
                diff_excerpt=diff_excerpt,
                plan_text=plan_text,
                log_path=self.log_path,
            )
            rec("rubric", rubric)
            enter("standards audit")
            standards = _tw.pre_pr_audit.run_standards_audit(
                cfg=self.cfg,
                handle=self._h,
                diff_excerpt=diff_excerpt,
                standards_text=standards_text,
                log_path=self.log_path,
            )
            rec("standards", standards)
            stages.extend([rubric, standards])
        enter("behavior audit")
        behavior = _tw.pre_pr_audit.run_behavior_audit(
            cfg=self.cfg,
            handle=self._h,
            expected_evidence=self._behavior_audit_expected_evidence(merge_node_mode=merge_node_mode),
            diff_excerpt=diff_excerpt,
            plan_text=plan_text,
            log_path=self.log_path,
        )
        rec("behavior", behavior)
        stages.append(behavior)
        return stages

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
