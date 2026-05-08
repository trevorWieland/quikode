"""Lifecycle tests for `qk daemon stop` / status / `qk reset` orphan handling.

The May 2026 incident: SIGTERM to the supervisor killed the supervisor but
left its `quikode.cli run` child reparented to init, ticking against a
workspace the operator believed was clean. These tests pin down the fixed
behavior:

  * `daemon stop` walks the full child tree, signals each process, waits up
    to a timeout, SIGKILLs anything still alive, and unconditionally removes
    pid + heartbeat files.
  * Orphan detection finds live `quikode.cli run` processes when supervisor
    is dead.
  * `qk reset` refuses to run while a daemon supervisor or orphan child is
    alive (escape hatch: `--force`).

Real `subprocess.Popen` is used (not mocks) — we need actual `/proc` entries
+ real signal delivery to exercise the pipeline. Helper processes are
launched as `python -c '...'` and reaped in addfinalizers.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from typer.testing import CliRunner

from quikode import daemon as daemon_mod
from quikode import process_tree
from quikode.cli import app
from quikode.config import Config


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
    )


def _spawn_sleep(duration_s: int = 60) -> subprocess.Popen:
    """Cooperative SIGTERM-honoring child: just `time.sleep`."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({duration_s})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_supervisor_with_child(duration_s: int = 60) -> subprocess.Popen:
    """Parent that forks a sleep child, then sleeps itself.

    Used to simulate the supervisor → child relationship the daemon-stop
    descendant walker is supposed to discover.
    """
    code = f"""
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({duration_s})"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep({duration_s})
"""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_sigterm_ignorer(duration_s: int = 30) -> subprocess.Popen:
    """Process that ignores SIGTERM for `duration_s` seconds, then exits.

    Reproduces the "child ignores SIGTERM" pattern that forces SIGKILL.
    """
    code = f"""
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep({duration_s})
"""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill_proc(p: subprocess.Popen) -> None:
    """Defensive teardown for tests that may leave Popen hanging."""
    if p.poll() is not None:
        return
    try:
        p.kill()
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _wait_until_alive(pid: int, timeout_s: float = 2.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process_tree.process_alive(pid):
            return
        time.sleep(0.05)


def _wait_until_dead(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not process_tree.process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _write_pid_file(cfg: Config, pid: int) -> None:
    """Plant a pid file pointing at `pid` so `stop_daemon` picks it up."""
    daemon_mod.daemon_pid_file(cfg).write_text(f"{pid}@{time.time():.0f}\n")


def _write_heartbeat(cfg: Config, ts: float | None = None) -> None:
    daemon_mod.heartbeat_file(cfg).write_text(
        f'{{"ts": {ts if ts is not None else time.time()}, "in_flight": 1, '
        f'"pending_ci": 0, "addressing_feedback": 0}}\n'
    )


# ----- discover_descendants -----


def test_discover_descendants_finds_child_tree(request):
    parent = _spawn_supervisor_with_child(duration_s=60)
    request.addfinalizer(lambda: _kill_proc(parent))
    _wait_until_alive(parent.pid)
    # Give the parent a moment to fork its child.
    time.sleep(0.5)

    descendants = process_tree.discover_descendants(parent.pid)
    pids = [d.pid for d in descendants]
    assert pids, "expected at least one descendant"
    # All descendants should currently be alive.
    for d in descendants:
        assert process_tree.process_alive(d.pid)


def test_discover_descendants_empty_for_leaf(request):
    leaf = _spawn_sleep(60)
    request.addfinalizer(lambda: _kill_proc(leaf))
    _wait_until_alive(leaf.pid)
    descendants = process_tree.discover_descendants(leaf.pid)
    assert descendants == []


# ----- stop_daemon: cooperative supervisor + child -----


def test_stop_daemon_terminates_supervisor_and_child(tmp_path, request):
    cfg = _make_cfg(tmp_path)
    sup = _spawn_supervisor_with_child(duration_s=60)
    request.addfinalizer(lambda: _kill_proc(sup))
    _wait_until_alive(sup.pid)
    time.sleep(0.5)  # let it fork

    descendants_before = process_tree.discover_descendants(sup.pid)
    assert descendants_before, "test setup: supervisor should have forked a child"

    _write_pid_file(cfg, sup.pid)
    _write_heartbeat(cfg)

    log_lines: list[str] = []
    ok = daemon_mod.stop_daemon(cfg, timeout_s=5, log_fn=log_lines.append)
    assert ok, f"stop_daemon should succeed; log: {log_lines}"

    # Both supervisor and at-least-one descendant must be dead.
    assert _wait_until_dead(sup.pid), "supervisor still alive"
    for d in descendants_before:
        assert _wait_until_dead(d.pid), f"descendant pid={d.pid} still alive"

    # Pid + heartbeat files removed.
    assert not daemon_mod.daemon_pid_file(cfg).exists()
    assert not daemon_mod.heartbeat_file(cfg).exists()

    # Logged a per-pid SIGTERM line for the supervisor.
    assert any("SIGTERM supervisor" in line for line in log_lines), log_lines


# ----- stop_daemon: non-cooperative child (SIGKILL fallback) -----


def test_stop_daemon_sigkills_uncooperative_child(tmp_path, request):
    cfg = _make_cfg(tmp_path)
    ignorer = _spawn_sigterm_ignorer(duration_s=30)
    request.addfinalizer(lambda: _kill_proc(ignorer))
    _wait_until_alive(ignorer.pid)
    # Give the python interpreter a moment to install the SIG_IGN handler.
    time.sleep(0.3)

    _write_pid_file(cfg, ignorer.pid)
    _write_heartbeat(cfg)

    log_lines: list[str] = []
    # Tight SIGTERM budget so the SIGKILL path fires fast.
    ok = daemon_mod.stop_daemon(cfg, timeout_s=2, log_fn=log_lines.append)
    assert ok, f"SIGKILL path should clean it up; log: {log_lines}"
    assert _wait_until_dead(ignorer.pid)
    assert not daemon_mod.daemon_pid_file(cfg).exists()
    assert not daemon_mod.heartbeat_file(cfg).exists()
    # Confirm we reached the SIGKILL branch.
    assert any("SIGKILL" in line for line in log_lines), log_lines


# ----- stop_daemon: dead pid file (cleanup path) -----


def test_stop_daemon_dead_pid_just_cleans_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    # Plant a pid file pointing at a long-dead pid (PID 1 is alive but a
    # safer pick is to fork+exit and use the resulting reaped pid… simpler:
    # use a guaranteed-dead pid by forking a no-op and waiting it out).
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    dead_pid = p.pid
    assert not process_tree.process_alive(dead_pid)

    _write_pid_file(cfg, dead_pid)
    _write_heartbeat(cfg)

    log_lines: list[str] = []
    ok = daemon_mod.stop_daemon(cfg, timeout_s=1, log_fn=log_lines.append)
    # Returns False (nothing to stop), but lifecycle files MUST be cleaned.
    assert ok is False
    assert not daemon_mod.daemon_pid_file(cfg).exists()
    assert not daemon_mod.heartbeat_file(cfg).exists()


# ----- detect_orphan_quikode_runs -----


def test_detect_orphan_quikode_runs_returns_empty_when_clean(tmp_path):
    cfg = _make_cfg(tmp_path)
    sup_proc, orphans = daemon_mod.detect_orphan_quikode_runs(cfg)
    # Test environment is unlikely to have a real `quikode.cli run` going.
    # We only assert the no-pid-file path returns no supervisor.
    assert sup_proc is None
    # Orphans may be present in unusual host states; assert the type only.
    assert isinstance(orphans, list)


def test_find_orphan_quikode_runs_matches_synthetic_cmdline(request, tmp_path, monkeypatch):
    """Spawn a process whose cmdline contains the inner-run pattern and
    verify the scanner finds it. Uses argv-style invocation so /proc
    cmdline reads back the markers we expect."""
    # We can't easily fake `/proc/<pid>/cmdline` without running a real
    # process. Spawn `python -c '...'` with an argv tail that matches the
    # detection regex.
    fake = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "--",
            "quikode.cli",
            "run",
            "--max-parallel",
            "12",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    request.addfinalizer(lambda: _kill_proc(fake))
    _wait_until_alive(fake.pid)

    matches = process_tree.find_orphan_quikode_runs()
    pids = [m.pid for m in matches]
    assert fake.pid in pids, f"expected synthetic process in matches; saw: {pids}"


# ----- daemon status: orphan detection via stop_daemon path -----


def test_stop_daemon_no_pid_file_reports_no_daemon(tmp_path):
    cfg = _make_cfg(tmp_path)
    # No pid file at all.
    ok = daemon_mod.stop_daemon(cfg, timeout_s=1, log_fn=lambda _msg: None)
    assert ok is False
    # No files to clean — but the call shouldn't fail.
    assert not daemon_mod.daemon_pid_file(cfg).exists()


# ----- qk reset: refusal under live process -----


def test_reset_refuses_when_daemon_supervisor_alive(tmp_path, request, monkeypatch):
    """If a supervisor pid is alive, `qk reset` must refuse with exit != 0."""
    cfg = _make_cfg(tmp_path)
    # Make load_config + daemon paths point at our tmp cfg.
    monkeypatch.setattr("quikode.cli_reset_plan.load_config", lambda: cfg)

    sup = _spawn_sleep(60)
    request.addfinalizer(lambda: _kill_proc(sup))
    _wait_until_alive(sup.pid)
    _write_pid_file(cfg, sup.pid)

    runner = CliRunner()
    # `--yes` so we never block on the confirm prompt; refusal happens BEFORE.
    result = runner.invoke(app, ["reset", "--yes"])
    assert result.exit_code != 0, result.output
    assert "refusing to reset" in result.output
    assert str(sup.pid) in result.output


def test_reset_force_bypasses_orphan_check(tmp_path, request, monkeypatch):
    """`--force` should let reset run even with a live supervisor.

    We don't actually run the destructive path (no docker / no real repo);
    we just assert the refusal branch is skipped, which surfaces as a
    different error (the destructive path failing on a missing repo).
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("quikode.cli_reset_plan.load_config", lambda: cfg)
    # Stub the destructive ops so the test isn't hostage to docker/git availability.
    monkeypatch.setattr("quikode.cli_reset_plan.docker_env.cleanup_all_quikode", lambda _cfg: 0)
    monkeypatch.setattr("quikode.cli_reset_plan._reset_worktrees_and_branches", lambda _cfg: None)
    monkeypatch.setattr(
        "quikode.cli_reset_plan._reset_db_and_logs",
        lambda _cfg, *, keep_db: None,
    )

    sup = _spawn_sleep(60)
    request.addfinalizer(lambda: _kill_proc(sup))
    _wait_until_alive(sup.pid)
    _write_pid_file(cfg, sup.pid)

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--yes", "--force"])
    # Force path runs through cleanly with the stubs above.
    assert result.exit_code == 0, result.output
    assert "refusing to reset" not in result.output


# ----- _signal_pid + _cleanup_lifecycle_files -----


def test_signal_pid_returns_false_for_dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert daemon_mod._signal_pid(p.pid, signal.SIGTERM) is False


def test_cleanup_lifecycle_files_idempotent(tmp_path):
    cfg = _make_cfg(tmp_path)
    daemon_mod.daemon_pid_file(cfg).write_text("1234@0\n")
    daemon_mod.heartbeat_file(cfg).write_text('{"ts": 0}\n')
    daemon_mod._cleanup_lifecycle_files(cfg)
    assert not daemon_mod.daemon_pid_file(cfg).exists()
    assert not daemon_mod.heartbeat_file(cfg).exists()
    # Second call must not raise.
    daemon_mod._cleanup_lifecycle_files(cfg)


# ----- _sigkill_order -----


def test_sigkill_order_supervisor_first_docker_last(tmp_path, monkeypatch):
    """supervisor → ordinary → docker. We fake `read_cmdline` to label pids."""
    fake_cmdlines = {
        100: "/usr/bin/python -m quikode.cli run --max-parallel 12",
        200: "docker exec qk-foo bash -lc 'cargo test'",
        300: "/usr/bin/python -m quikode.workers.subtask",
    }
    monkeypatch.setattr("quikode.daemon_shutdown.read_cmdline", lambda pid: fake_cmdlines.get(pid, ""))
    order = daemon_mod._sigkill_order(supervisor_pid=100, survivors=[100, 200, 300])
    assert order == [100, 300, 200]


# ----- regression marker -----


def test_orphan_detection_excludes_passed_pids(request):
    """The `exclude_pids` filter in find_orphan_quikode_runs must keep the
    supervisor itself from showing up as its own orphan."""
    fake = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "quikode.cli",
            "run",
            "--max-parallel",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    request.addfinalizer(lambda: _kill_proc(fake))
    _wait_until_alive(fake.pid)

    matches = process_tree.find_orphan_quikode_runs(exclude_pids={fake.pid})
    assert fake.pid not in [m.pid for m in matches]


# Quick sanity: the test module's own pid is filtered out of orphan scan.
def test_orphan_scan_excludes_self():
    matches = process_tree.find_orphan_quikode_runs()
    assert os.getpid() not in [m.pid for m in matches]
