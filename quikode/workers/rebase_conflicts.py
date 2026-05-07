"""Rebase conflict resolution worker mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class RebaseConflictMixin:
    def _rebase_or_resolve(self: Any) -> WorkerOutcome | None:
        fsm_runtime.enter_rebasing_to_main(self.store, self.node.id)
        rc, out = self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        if rc != 0:
            fsm_runtime.block_current(self.store, self.node.id, note=f"git fetch failed: {out[:300]}")
            return WorkerOutcome(State.BLOCKED, "git fetch failed before rebase")

        rc, out = self._git_in_workspace(["rebase", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"])
        if rc == 0:
            self.store.set_field(
                self.node.id,
                last_synced_main_sha=self._git_in_workspace(
                    ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
                )[1].strip()
                or None,
            )
            branch_str = str(self._row()["branch"])
            ahead = self._git_ahead_count(branch_str)
            if ahead == 0:
                row_now = self._row()
                worktree_path = row_now.get("worktree_path") or ""
                note = (
                    f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} - "
                    f"the rebase likely dropped task changes. Inspect the worktree at {worktree_path} before retrying."
                )
                fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
                return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")
            push_rc, _push_out = _tw.github.push(
                self._h,
                str(self._row()["branch"]),
                remote=self.cfg.pr_remote,
                log_path=self.log_path,
            )
            if push_rc != 0:
                rc2, out2 = self._git_in_workspace(
                    ["push", "--force-with-lease", "-u", self.cfg.pr_remote, str(self._row()["branch"])]
                )
                if rc2 != 0:
                    fsm_runtime.block_current(
                        self.store,
                        self.node.id,
                        note=f"force-push after rebase failed: {out2[:300]}",
                    )
                    return WorkerOutcome(State.BLOCKED, "rebase push failed")
            return None

        self.store.increment(self.node.id, "conflict_resolve_retries")
        return self._spawn_conflict_resolver()

    def _spawn_conflict_resolver(self: Any) -> WorkerOutcome | None:
        fsm_runtime.enter_conflict_resolving(self.store, self.node.id)
        max_iterations = 6
        for iteration in range(1, max_iterations + 1):
            outcome = self._resolve_one_conflict_step(iteration=iteration)
            if outcome is not None:
                return outcome
            if not self._rebase_in_progress():
                break
        else:
            self._git_in_workspace(["rebase", "--abort"])
            self._ensure_on_branch()
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"conflict resolver exceeded {max_iterations} iterations; aborting",
            )
            return WorkerOutcome(State.BLOCKED, "conflict iteration cap")

        verify_cmd = (self.cfg.local_ci_command or "").strip()
        if not verify_cmd:
            verify_cmd = "true"
        rc, out, err = _tw.exec_in(
            self._h,
            ["bash", "-lc", f"cd /workspace && {verify_cmd} 2>&1"],
            log_path=self.log_path,
            timeout=self.cfg.local_ci_timeout_s,
        )
        if rc != 0:
            ci_log = (out or "") + "\n" + (err or "")
            self.store.add_artifact(self.node.id, "post_rebase_ci_log", ci_log)
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"post-rebase `{verify_cmd}` FAILed; conflict resolution broke build",
                last_error=_tw._last_lines(ci_log, 30)[:1000],
            )
            return WorkerOutcome(State.BLOCKED, "rebase verify failed")

        branch_str = str(self._row()["branch"])
        ahead = self._git_ahead_count(branch_str)
        if ahead == 0:
            row_now = self._row()
            worktree_path = row_now.get("worktree_path") or ""
            note = (
                f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} - "
                f"the rebase likely dropped task changes. Inspect the worktree at {worktree_path} before retrying."
            )
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")
        rc, out = self._git_in_workspace(
            ["push", "--force-with-lease", "-u", self.cfg.pr_remote, str(self._row()["branch"])]
        )
        if rc != 0:
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"force-push after resolution failed: {out[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase resolved but push failed")
        return None

    def _resolve_one_conflict_step(self: Any, *, iteration: int) -> WorkerOutcome | None:
        row = self._row()
        base_sha = row.get("base_ref_sha") or "HEAD~1"
        _, task_diff = self._git_in_workspace(["diff", f"{base_sha}...HEAD", "--no-color"])
        _, main_log = self._git_in_workspace(
            ["log", "--oneline", f"{base_sha}..{self.cfg.pr_remote}/{self.cfg.base_branch}"]
        )
        _, main_diff = self._git_in_workspace(
            ["diff", f"{base_sha}..{self.cfg.pr_remote}/{self.cfg.base_branch}", "--no-color"]
        )
        _, status_out = self._git_in_workspace(["diff", "--name-only", "--diff-filter=U"])
        conflicted: list[dict] = []
        for path in status_out.splitlines():
            path = path.strip()
            if not path:
                continue
            _rc, marked, _err = _tw.exec_in(
                self._h,
                ["bash", "-lc", f"cat /workspace/{path}"],
                log_path=self.log_path,
                timeout=30,
            )
            conflicted.append({"path": path, "content": marked[:3000]})
        if not conflicted:
            rc_cont, out_cont = self._git_in_workspace(["-c", "core.editor=true", "rebase", "--continue"])
            if rc_cont == 0:
                return None
            self._git_in_workspace(["rebase", "--abort"])
            self._ensure_on_branch()
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"rebase iter {iteration} failed; no conflicts surfaced and --continue rc={rc_cont}: {out_cont[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase abort")

        agent = _tw.build_agent(self.cfg.conflict_resolver)
        prompt = _tw.prompts.conflict_resolver_prompt(
            self.cfg,
            self.node,
            task_diff_excerpt=task_diff,
            main_log_excerpt=main_log,
            main_diff_excerpt=main_diff,
            conflicted_files=conflicted,
        )
        self._write_log_header(f"CONFLICT RESOLVER (iter {iteration})", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
        self.store.record_agent_call(
            self.node.id,
            phase="conflict_resolver",
            cli=self.cfg.conflict_resolver.cli,
            model=self.cfg.conflict_resolver.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        self.store.add_artifact(self.node.id, f"conflict_resolver_output_iter{iteration}", result.stdout)

        if "GIVE_UP:" in result.stdout:
            self._git_in_workspace(["rebase", "--abort"])
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"conflict resolver gave up at iter {iteration}; needs human resolution",
            )
            return WorkerOutcome(State.BLOCKED, "conflict resolver gave up")

        self._git_in_workspace(["add", "-A"])
        rc, out = self._git_in_workspace(["-c", "core.editor=true", "rebase", "--continue"])
        if rc != 0 and not self._rebase_in_progress():
            self._git_in_workspace(["rebase", "--abort"])
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"rebase --continue at iter {iteration} failed: {out[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase --continue failed")
        return None

    def _rebase_in_progress(self: Any) -> bool:
        for kind in ("rebase-merge", "rebase-apply"):
            rc, out = self._git_in_workspace(["rev-parse", "--git-path", kind])
            if rc != 0:
                continue
            path = (out.strip().splitlines() or [""])[-1].strip()
            if not path:
                continue
            rc2, _out, _err = _tw.exec_in(
                self._h,
                ["bash", "-lc", f"cd /workspace && test -d {_tw.shlex.quote(path)}"],
                log_path=self.log_path,
                timeout=15,
            )
            if rc2 == 0:
                return True
        return False

    def _ensure_on_branch(self: Any, branch: str | None = None) -> None:
        rc, _out = self._git_in_workspace(["symbolic-ref", "--short", "-q", "HEAD"])
        if rc == 0:
            return
        if branch is None:
            row = self.store.get(self.node.id) or {}
            branch = str(row.get("branch") or "")
        if not branch:
            return
        self._git_in_workspace(["symbolic-ref", "HEAD", f"refs/heads/{branch}"])

    def _git_in_workspace(self: Any, args: list[str]) -> tuple[int, str]:
        rc, out, err = _tw.exec_in(
            self._h,
            ["bash", "-lc", "cd /workspace && git " + " ".join(args)],
            log_path=self.log_path,
            timeout=300,
        )
        return rc, (out + err)

    def _git_ahead_count(self: Any, branch: str, base: str | None = None) -> int:
        base_branch = base or self.cfg.base_branch
        rc, out = self._git_in_workspace(
            ["rev-list", "--count", f"{self.cfg.pr_remote}/{base_branch}..{branch}"]
        )
        if rc != 0:
            return 0
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0
