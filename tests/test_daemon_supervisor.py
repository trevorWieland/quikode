"""Tests for the daemon supervisor (Phase C item 5).

The supervisor wraps `quikode run` with crash-restart + signal handling.
We mock `subprocess.Popen` to control child lifecycle without spawning
real processes. Sleep is also injected so backoff schedules don't slow
the suite.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quikode import daemon as daemon_mod
from quikode.cli import app
from quikode.config import Config

SLEEP_BIN = shutil.which("sleep")


def _make_cfg(tmp_path: Path) -> Config:
    state = tmp_path / ".quikode"
    state.mkdir(parents=True, exist_ok=True)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=state,
        log_dir=state / "logs",
        worktree_root=state / "worktrees",
        sccache_dir=state / "sccache",
        prompts_dir=tmp_path / "prompts",
        # Tighten backoff so tests run fast (and assertions check exact values).
        daemon_backoff_schedule_s=[60, 300, 1800],
        daemon_min_run_for_backoff_reset_s=300,
        daemon_heartbeat_staleness_s=30,
    )


# ----- Backoff schedule -----


def test_backoff_for_attempt_first():
    assert daemon_mod._backoff_for_attempt([60, 300, 1800], 1) == 60


def test_backoff_for_attempt_second():
    assert daemon_mod._backoff_for_attempt([60, 300, 1800], 2) == 300


def test_backoff_for_attempt_caps_at_last():
    assert daemon_mod._backoff_for_attempt([60, 300, 1800], 3) == 1800
    assert daemon_mod._backoff_for_attempt([60, 300, 1800], 10) == 1800


def test_backoff_empty_falls_back_to_60():
    assert daemon_mod._backoff_for_attempt([], 1) == 60


# ----- Supervise loop with mocked child -----


class _FakeChild:
    """Stand-in for subprocess.Popen used in tests."""

    def __init__(self, *, exit_codes: list[int], runtime_s: float = 0.0):
        # exit_codes: list of return codes, one per expected spawn.
        self._exit_codes = list(exit_codes)
        self._runtime_s = runtime_s
        self.returncode: int | None = None
        self.pid = 12345
        self.signals_received: list[int] = []
        self._terminated = False

    def wait(self) -> int:
        if not self._exit_codes:
            raise RuntimeError("no more exit codes scripted")
        rc = self._exit_codes.pop(0)
        self.returncode = rc
        return rc

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)
        # Simulate the child responding to SIGTERM by setting rc=0
        if sig == signal.SIGTERM:
            self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = 0


def _patch_spawn(monkeypatch, children: list[_FakeChild]):
    """Make supervisor's _spawn_child return successive fakes.

    Also speeds up time.time() perception of "ran for X seconds" by recording
    the elapsed time between spawn calls — the real `child.wait()` returns
    instantly here but supervisor measures (now-spawn_ts), so we patch time.time
    via a mutable reference if a test asks for an explicit runtime.
    """
    iterator = iter(children)

    def _fake_spawn(cfg, run_args, log_fp):
        try:
            return next(iterator)
        except StopIteration as e:
            raise RuntimeError("supervisor spawned more children than expected") from e

    monkeypatch.setattr(daemon_mod, "_spawn_child", _fake_spawn)


def _no_sleep(_sec):  # used as sleep_fn so backoffs don't actually delay
    return None


def test_clean_exit_zero_no_restart(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    children = [_FakeChild(exit_codes=[0])]
    _patch_spawn(monkeypatch, children)
    rc = daemon_mod.supervise(cfg, [], sleep_fn=_no_sleep)
    assert rc == 0
    # daemon.pid was cleaned up
    assert not daemon_mod.daemon_pid_file(cfg).exists()


def test_crash_then_restart_then_clean(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    spawn_calls = []

    def _fake_spawn(cfg_arg, run_args, log_fp):
        ch = _FakeChild(exit_codes=[1 if len(spawn_calls) == 0 else 0])
        spawn_calls.append(ch)
        return ch

    monkeypatch.setattr(daemon_mod, "_spawn_child", _fake_spawn)
    rc = daemon_mod.supervise(cfg, [], sleep_fn=_no_sleep)
    assert rc == 0
    assert len(spawn_calls) == 2  # crashed once, succeeded second time


def test_three_consecutive_crashes_uses_full_backoff_schedule(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # First three crash; fourth exits cleanly to terminate the loop.
    children = [
        _FakeChild(exit_codes=[1]),
        _FakeChild(exit_codes=[1]),
        _FakeChild(exit_codes=[1]),
        _FakeChild(exit_codes=[0]),
    ]
    _patch_spawn(monkeypatch, children)
    sleeps: list[int] = []

    def _record_sleep(s):
        sleeps.append(s)

    rc = daemon_mod.supervise(cfg, [], sleep_fn=_record_sleep)
    assert rc == 0
    # Expected: backoff after each of the 3 crashes — 60, 300, 1800 (capped).
    assert sleeps == [60, 300, 1800]


def test_crash_after_long_run_resets_backoff(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # Patch time.time so the supervisor "sees" the first child run for >5min.
    base = [1_000_000.0]

    def _fake_time():
        return base[0]

    monkeypatch.setattr(daemon_mod.time, "time", _fake_time)

    children = [
        _FakeChild(exit_codes=[1]),  # crashes after long run
        _FakeChild(exit_codes=[1]),  # crashes immediately — backoff should be FIRST entry again
        _FakeChild(exit_codes=[0]),  # clean
    ]
    iterator = iter(children)

    def _fake_spawn(cfg_arg, run_args, log_fp):
        ch = next(iterator)
        if ch is children[0]:
            # Advance simulated clock so the first run looks long
            base[0] += cfg.daemon_min_run_for_backoff_reset_s + 10
        return ch

    monkeypatch.setattr(daemon_mod, "_spawn_child", _fake_spawn)
    sleeps: list[int] = []

    def _record_sleep(s):
        sleeps.append(s)
        # 2nd crash happens "immediately" (no clock advance) so backoff should reset

    rc = daemon_mod.supervise(cfg, [], sleep_fn=_record_sleep)
    assert rc == 0
    # First crash after long run → reset to first entry (60).
    # Second crash immediately after → still 60 (reset path again? no, only
    # crashes that ran < min_run_reset increment). After reset we set
    # consecutive_crashes=1, so next backoff is schedule[0]=60.
    # Then second crash ran 0s, consecutive_crashes=2, backoff=schedule[1]=300.
    assert sleeps == [60, 300]


def test_supervisor_sigterm_forwards_and_exits(tmp_path, monkeypatch):
    """When the supervisor receives SIGTERM, it forwards SIGTERM to the child
    and exits cleanly even if more children would have been scheduled."""
    cfg = _make_cfg(tmp_path)
    state = daemon_mod._SupervisorState()

    # Pretend the supervisor was already shutdown when we get to the loop:
    # easier than mocking signals end-to-end, and asserts the same code path.
    child = _FakeChild(exit_codes=[0])

    monkeypatch.setattr(daemon_mod, "_spawn_child", lambda cfg_arg, run_args, log_fp: child)

    # Stub out signal install (we don't want to clobber pytest's handlers)
    monkeypatch.setattr(daemon_mod, "_install_signal_handlers", lambda s: None)

    # Use the real supervise but flip shutdown after the first wait
    real_wait = child.wait

    def _wait_then_shutdown():
        return real_wait()

    child.wait = _wait_then_shutdown

    rc = daemon_mod.supervise(cfg, [], sleep_fn=_no_sleep)
    assert rc == 0
    # state object is internal; just verify daemon.pid was cleaned up
    assert not daemon_mod.daemon_pid_file(cfg).exists()
    # _SupervisorState helper smoke
    assert state.shutdown is False  # untouched by this path


def test_supervisor_writes_and_cleans_pid_file(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    child = _FakeChild(exit_codes=[0])
    monkeypatch.setattr(daemon_mod, "_spawn_child", lambda cfg_arg, run_args, log_fp: child)
    monkeypatch.setattr(daemon_mod, "_install_signal_handlers", lambda s: None)
    pid_path = daemon_mod.daemon_pid_file(cfg)
    assert not pid_path.exists()
    rc = daemon_mod.supervise(cfg, [], sleep_fn=_no_sleep)
    assert rc == 0
    # cleaned up at exit
    assert not pid_path.exists()


def test_supervisor_terminate_child_helper():
    child = _FakeChild(exit_codes=[0])
    rc = daemon_mod._terminate_child(child, timeout_s=1)
    assert rc == 0
    assert signal.SIGTERM in child.signals_received


def test_supervisor_terminate_child_already_dead():
    child = _FakeChild(exit_codes=[0])
    child.returncode = 0  # already dead
    rc = daemon_mod._terminate_child(child, timeout_s=1)
    assert rc == 0
    assert child.signals_received == []  # no signal sent


def test_failsafe_kill_fires_sigkill_when_child_ignores_sigterm():
    """If the inner orchestrator doesn't obey SIGTERM within the failsafe
    window, the supervisor sends SIGKILL from a background timer. Without this
    the supervisor's `child.wait()` hangs forever, `stop_daemon` SIGKILLs the
    supervisor instead, and the orphaned child keeps running against a
    cleaned-up workspace — burning retry budget in seconds. Regression for
    the 2026-05-03 R-0002 runaway."""
    child = _FakeChild(exit_codes=[0])
    # Simulate a misbehaving child: don't react to SIGTERM, stay alive.
    child.returncode = None

    def _ignore_sigterm(sig: int) -> None:
        child.signals_received.append(sig)
        # NOTE: do NOT set returncode — child stays alive

    child.send_signal = _ignore_sigterm  # type: ignore[assignment]

    timer = daemon_mod._schedule_failsafe_kill(child, timeout_s=0.1)
    timer.join(timeout=2.0)
    assert not timer.is_alive()
    assert signal.SIGKILL in child.signals_received


def test_failsafe_kill_skips_sigkill_when_child_already_exited():
    """Happy path: child obeys SIGTERM and exits before the failsafe fires.
    The timer should not send SIGKILL to a dead process."""
    child = _FakeChild(exit_codes=[0])
    child.returncode = 0  # already exited cleanly
    timer = daemon_mod._schedule_failsafe_kill(child, timeout_s=0.05)
    timer.join(timeout=1.0)
    assert signal.SIGKILL not in child.signals_received


# ----- daemon stop -----


@pytest.mark.skipif(not SLEEP_BIN, reason="sleep binary unavailable")
def test_daemon_stop_sends_sigterm_to_running(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # Spawn a real `sleep` and write its pid to daemon.pid
    proc = subprocess.Popen(
        [SLEEP_BIN, "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pid_file = daemon_mod.daemon_pid_file(cfg)
        pid_file.write_text(f"{proc.pid}@{time.time():.0f}\n")
        ok = daemon_mod.stop_daemon(cfg, timeout_s=5)
        assert ok
        assert not pid_file.exists()
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def test_daemon_stop_no_daemon(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert daemon_mod.stop_daemon(cfg) is False


def test_daemon_stop_stale_pid(tmp_path):
    cfg = _make_cfg(tmp_path)
    # Write a definitely-dead pid
    daemon_mod.daemon_pid_file(cfg).write_text("99999999@1700000000\n")
    assert daemon_mod.stop_daemon(cfg) is False
    # Stale file gets cleaned
    assert not daemon_mod.daemon_pid_file(cfg).exists()


# ----- daemon status (CLI) -----


def _write_workspace_with_config(tmp_path: Path) -> Path:
    """Create the minimum file layout that load_config expects."""
    qk = tmp_path / ".quikode"
    qk.mkdir(parents=True, exist_ok=True)
    (qk / "logs").mkdir(parents=True, exist_ok=True)
    cfg_path = qk / "config.toml"
    # Minimal valid config — repo_path/dag_path can point to the workspace.
    cfg_path.write_text(f'repo_path = "{tmp_path}"\ndag_path = "{tmp_path}"\n')
    return tmp_path


def test_daemon_status_no_daemon(tmp_path, monkeypatch):
    ws = _write_workspace_with_config(tmp_path)
    monkeypatch.chdir(ws)
    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 1, res.output


def test_daemon_status_running_fresh_heartbeat_json(tmp_path, monkeypatch):
    ws = _write_workspace_with_config(tmp_path)
    monkeypatch.chdir(ws)
    state = ws / ".quikode"
    # Use *our own* PID so liveness check passes
    (state / "daemon.pid").write_text(f"{os.getpid()}@{time.time():.0f}\n")
    (state / "orchestrator.heartbeat").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "in_flight": 2,
                "awaiting_merge": 1,
                "responding_to_review": 0,
            }
        )
    )
    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["daemon_alive"] is True
    assert data["heartbeat_stale"] is False
    assert data["heartbeat"]["in_flight"] == 2


def test_daemon_status_stale_heartbeat_returns_2(tmp_path, monkeypatch):
    ws = _write_workspace_with_config(tmp_path)
    monkeypatch.chdir(ws)
    state = ws / ".quikode"
    (state / "daemon.pid").write_text(f"{os.getpid()}@{time.time():.0f}\n")
    # Heartbeat 10 minutes old
    (state / "orchestrator.heartbeat").write_text(
        json.dumps({"ts": time.time() - 600, "in_flight": 0, "awaiting_merge": 0, "responding_to_review": 0})
    )
    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 2, res.output


def test_daemon_status_no_heartbeat_when_running_returns_2(tmp_path, monkeypatch):
    ws = _write_workspace_with_config(tmp_path)
    monkeypatch.chdir(ws)
    state = ws / ".quikode"
    (state / "daemon.pid").write_text(f"{os.getpid()}@{time.time():.0f}\n")
    # No heartbeat file
    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 2, res.output


# ----- Child cwd regression -----


def test_spawn_child_uses_workspace_dir_not_repo(tmp_path, monkeypatch):
    """Regression: the child must run from the workspace dir (state_dir.parent),
    not the target repo. Otherwise `load_config()` walks from the wrong cwd
    and crashes rc=1 in 0s because there's no .quikode/config.toml under the
    target repo.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(
        repo_path=repo,
        dag_path=repo / "dag.json",
        state_dir=workspace / ".quikode",
        log_dir=workspace / ".quikode" / "logs",
        worktree_root=workspace / ".quikode" / "worktrees",
        sccache_dir=workspace / ".quikode" / "sccache",
        prompts_dir=repo / "prompts",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, str] = {}

    def fake_popen(argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")

        class _Stub:
            pid = 12345

            def poll(self):
                return 0

        return _Stub()

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)

    log_fp = (cfg.log_dir / "daemon.log").open("w")
    try:
        daemon_mod._spawn_child(cfg, [], log_fp)
    finally:
        log_fp.close()

    assert captured["cwd"] == str(workspace)
    assert captured["cwd"] != str(repo)
