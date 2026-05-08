"""Subtask completion and operator-surfacing mixin."""

from __future__ import annotations

import sys
from typing import Any, Literal

from quikode.state import SubtaskState
from quikode.subtask_schema import Subtask
from quikode.workers.outcomes import SubtaskPassOutcome as _SubtaskPassOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class SubtaskCompletionMixin:
    def _mark_subtask_done(self: Any, subtask: Subtask) -> None:
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DONE.value)

    def _handle_subtask_pass(self: Any, subtask: Subtask) -> _SubtaskPassOutcome:
        gate_ok, gate_output = self._pre_commit_gate(subtask)
        if not gate_ok:
            self.store.increment_subtask_pre_commit_failures(self.node.id, subtask.id)
            checker_text = (
                f"VERDICT: FAIL\nROOT_CAUSE: pre-commit hook failed\nDETAILS:\n{gate_output[:4000]}"
            )
            return _SubtaskPassOutcome(kind="fail", synthesized_checker_text=checker_text)

        branch = str(self._row()["branch"])
        commit_msg = f"subtask({subtask.id}): {subtask.title}"

        # Plan 33 D5: scope-review retired. `commit_subtask` no longer
        # adjudicates lane drift — `files_to_touch` is advisory; the
        # audit gauntlet is the truth. The `_classify_empty_staging`
        # helper survives in worktree.py for Z-99's gate-only success
        # path.
        result = _tw.worktree.commit_subtask(
            self._h,
            subtask,
            commit_msg,
            branch=branch,
            remote=self.cfg.pr_remote,
            push=True,
            log_path=self.log_path,
        )
        if not result.success:
            if result.transient:
                self.store.increment_subtask_transient_retries(self.node.id, subtask.id)
                _tw.log.warning(
                    "subtask %s/%s: transient push failure; free-retrying. output: %s",
                    self.node.id,
                    subtask.id,
                    result.output[:300],
                )
                return _SubtaskPassOutcome(kind="transient_retry")
            checker_text = f"VERDICT: FAIL\nROOT_CAUSE: commit/push failed\nDETAILS:\n{result.output[:4000]}"
            return _SubtaskPassOutcome(kind="fail", synthesized_checker_text=checker_text)

        update_fields: dict[str, Any] = {"commit_sha": result.commit_sha}
        if result.accepted_files and set(result.accepted_files) != set(subtask.files_to_touch):
            update_fields["accepted_files"] = ",".join(result.accepted_files)
        self.store.update_subtask(self.node.id, subtask.id, **update_fields)
        self._mark_subtask_done(subtask)
        return _SubtaskPassOutcome(kind="settled")

    def _pre_commit_gate(self: Any, subtask: Subtask) -> tuple[bool, str]:
        runner = self.cfg.pre_commit_runner
        if runner == "none" or not subtask.files_to_touch:
            return True, "skipped"

        if runner == "auto":
            resolved = self._detect_pre_commit_runner()
            if resolved is None:
                return True, "no hook configured"
            runner = resolved

        if runner == "lefthook":
            cmd = "cd /workspace && lefthook run pre-commit --files-from-stdin"
            stdin = "\n".join(subtask.files_to_touch)
        elif runner == "pre-commit":
            files_arg = " ".join(_tw.shlex.quote(p) for p in subtask.files_to_touch)
            cmd = f"cd /workspace && pre-commit run --files {files_arg}"
            stdin = None
        else:
            return True, f"unknown runner {runner!r}; skipped"

        try:
            rc, out, err = _tw.exec_in(
                self._h,
                ["bash", "-lc", cmd],
                log_path=self.log_path,
                stdin=stdin,
                timeout=self.cfg.pre_commit_timeout_s,
            )
        except _tw.subprocess.TimeoutExpired:
            return False, f"pre-commit timed out after {self.cfg.pre_commit_timeout_s}s"

        combined = (out or "") + ("\n" + err if err else "")
        return rc == 0, combined

    def _detect_pre_commit_runner(self: Any) -> Literal["lefthook", "pre-commit"] | None:
        rc, _, _ = _tw.exec_in(
            self._h,
            ["bash", "-lc", "test -f /workspace/lefthook.yml || test -f /workspace/lefthook.yaml"],
            log_path=self.log_path,
            timeout=10,
        )
        if rc == 0:
            return "lefthook"
        rc, _, _ = _tw.exec_in(
            self._h,
            ["bash", "-lc", "test -f /workspace/.pre-commit-config.yaml"],
            log_path=self.log_path,
            timeout=10,
        )
        if rc == 0:
            return "pre-commit"
        return None

    def _mark_subtask_blocked(self: Any, subtask: Subtask, reason: str) -> None:
        self.store.update_subtask(
            self.node.id,
            subtask.id,
            state=SubtaskState.BLOCKED.value,
            last_error=reason,
        )
        _tw.log.warning("subtask %s/%s blocked: %s", self.node.id, subtask.id, reason)
        try:
            self._post_blocked_pr_comment(subtask, reason)
        except Exception:
            _tw.log.exception("failed to post BLOCKED PR comment for %s/%s", self.node.id, subtask.id)

    def _post_blocked_pr_comment(self: Any, subtask: Subtask, reason: str) -> None:
        row = self.store.get(self.node.id) or {}
        pr_number = row.get("draft_pr_number") or row.get("pr_number")
        if not pr_number:
            return
        try:
            pr_number_int = int(pr_number)
        except (TypeError, ValueError):
            return

        try:
            attempts = self._recent_attempt_history(subtask, n=3)
        except Exception:
            attempts = []
        attempts_section = ""
        if attempts:
            lines = []
            for a in attempts:
                rc = (a.checker_root_cause or "(no checker output captured)").strip()
                lines.append(f"- attempt {a.attempt_no}: {rc[:300]}")
            attempts_section = "\n\nLast {} attempts:\n{}".format(len(attempts), "\n".join(lines))

        worktree_path = row.get("worktree_path") or "(unknown - see `quikode show`)"
        body = (
            f"## Blocked at {subtask.id}\n\n"
            f"Reason: {reason}{attempts_section}\n\n"
            "To unblock, choose one:\n"
            f"1. **Push fixes to this branch directly.** Daemon detects new commits and resumes from {subtask.id}.\n"
            "2. **Reply with guidance** as a review comment on this PR. Daemon picks it up via the review loop.\n"
            f"3. **Locally**: `quikode unblock {self.node.id}` (opens worktree at {worktree_path}) "
            f"then `quikode resume {self.node.id}`.\n"
        )

        try:
            _tw.subprocess.run(
                ["gh", "pr", "comment", str(pr_number_int), "--body", body],
                cwd=str(self.cfg.repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (_tw.subprocess.SubprocessError, OSError) as e:
            _tw.log.warning("gh pr comment failed for #%d: %s", pr_number_int, e)
        try:
            _tw.subprocess.run(
                ["gh", "pr", "edit", str(pr_number_int), "--add-label", "quikode:blocked"],
                cwd=str(self.cfg.repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (_tw.subprocess.SubprocessError, OSError) as e:
            _tw.log.warning("gh pr edit add-label failed for #%d: %s", pr_number_int, e)

    def _mark_subtask_skipped(self: Any, subtask: Subtask, reason: str) -> None:
        self.store.update_subtask(
            self.node.id,
            subtask.id,
            state=SubtaskState.SKIPPED.value,
            last_error=reason,
        )
        _tw.log.info("subtask %s/%s skipped: %s", self.node.id, subtask.id, reason)

    def _mark_remaining_pending_as_skipped(
        self: Any, *, after: str, subtasks: list[Subtask] | None = None
    ) -> None:
        if subtasks is None:
            assert self.plan is not None
            subtasks = self.plan.topo_order()
        seen_marker = False
        for subtask in subtasks:
            if subtask.id == after:
                seen_marker = True
                continue
            if not seen_marker:
                continue
            existing = self.store.get_subtask(self.node.id, subtask.id)
            if existing and existing["state"] == SubtaskState.PENDING.value:
                self._mark_subtask_skipped(subtask, f"upstream subtask {after} blocked")
