"""Manual-probe runner tests.

Covers:
  - Parsing structured + free-text `expected_evidence` items into
    `ManualProbe`.
  - Substitution of `$PORT_<service>` placeholders.
  - Classification logic (matched/mismatched/error).
  - Service start + teardown lifecycle with a mocked `exec_in`.
  - Worker integration: `_run_manual_probes` emits a non-empty prompt
    block when the node has manual evidence and the runner returns
    matched results.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.docker_env import make_handle
from quikode.manual_probe import (
    ManualProbe,
    ManualProbeRunner,
    ProbeResult,
    collect_probes_from_evidence,
    parse_evidence_to_probe,
    render_probe_block,
)
from quikode.state import Store
from quikode.worker import TaskWorker

# ----- parsing ---------------------------------------------------------------


def test_parse_structured_evidence():
    item = {
        "kind": "manual",
        "service": "tanren-mcp",
        "command": "curl -s http://localhost:$PORT_tanren-mcp/health",
        "expected": "ok",
        "description": "MCP health check",
    }
    p = parse_evidence_to_probe(item)
    assert p is not None
    assert p.service == "tanren-mcp"
    assert p.command.startswith("curl")
    assert p.expected == "ok"
    assert p.description == "MCP health check"


def test_parse_free_text_evidence_extracts_curl_and_expected():
    """Free-text evidence falls back to regex extraction of curl + expected."""
    item = {
        "kind": "manual",
        "description": (
            'Run curl -sf http://localhost:8080/v1/health and verify the response returns "healthy"'
        ),
    }
    p = parse_evidence_to_probe(item)
    assert p is not None
    assert "curl" in p.command
    assert p.expected == "healthy"


def test_parse_skips_non_manual_kind():
    assert parse_evidence_to_probe({"kind": "test"}) is None
    assert parse_evidence_to_probe({"kind": "code"}) is None


def test_parse_returns_none_on_unrecoverable_malformed():
    """Item with kind=manual but no command and no description we can
    recover from → None (logged warning, not a crash)."""
    assert parse_evidence_to_probe({"kind": "manual", "description": ""}) is None
    assert parse_evidence_to_probe({"kind": "manual", "description": "no command here"}) is None


def test_parse_handles_non_dict_input():
    """Passing a non-dict (which can happen if expected_evidence is
    malformed) is logged + skipped, never raised."""
    assert parse_evidence_to_probe("a string") is None
    assert parse_evidence_to_probe(None) is None


def test_collect_probes_filters_and_skips():
    evidence = [
        {"kind": "test"},  # not manual
        {"kind": "manual", "command": "curl one"},
        {"kind": "manual", "description": "no command here"},  # malformed → skip
        {"kind": "manual", "command": "curl two", "expected": "x"},
    ]
    probes = collect_probes_from_evidence(evidence)
    assert len(probes) == 2
    assert probes[0].command == "curl one"
    assert probes[1].command == "curl two"


# ----- runner: classification ------------------------------------------------


def test_classify_match_with_substring():
    p = ManualProbe(command="curl x", expected="ok")
    assert ManualProbeRunner._classify(0, "all is ok here", p) is True
    assert ManualProbeRunner._classify(0, "fail", p) is False
    assert ManualProbeRunner._classify(1, "ok", p) is False  # rc != 0


def test_classify_no_expected_passes_on_nonempty_output():
    p = ManualProbe(command="curl x", expected="")
    assert ManualProbeRunner._classify(0, "any output", p) is True
    assert ManualProbeRunner._classify(0, "  \n", p) is False
    assert ManualProbeRunner._classify(0, "", p) is False


def test_classify_regex():
    p = ManualProbe(command="curl x", expected=r"^\{\"status\":\s*\"ok\"", expected_is_regex=True)
    assert ManualProbeRunner._classify(0, '{"status": "ok"}', p) is True
    assert ManualProbeRunner._classify(0, '{"status": "fail"}', p) is False


def test_classify_invalid_regex_falls_back_to_substring():
    p = ManualProbe(command="curl x", expected="(unclosed", expected_is_regex=True)
    # Falls back to substring → "(unclosed" not in "ok" → False
    assert ManualProbeRunner._classify(0, "ok", p) is False
    assert ManualProbeRunner._classify(0, "literal (unclosed string", p) is True


# ----- runner: service start / teardown --------------------------------------


def _make_exec_in_stub() -> MagicMock:
    """Build a configurable exec_in stub. Default returns rc=0 / empty.

    Tests can `set_responses({"some-cmd": (rc, out, err), ...})` keyed
    on a substring of the command to control specific calls.
    """
    stub = MagicMock()
    responses: dict[str, tuple[int, str, str]] = {}

    def _exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        for needle, resp in responses.items():
            if needle in joined:
                return resp
        return (0, "", "")

    stub.side_effect = _exec
    stub.responses = responses
    return stub


def test_runner_start_service_allocates_port_and_runs_in_background():
    exec_stub = _make_exec_in_stub()
    # Make `cat /tmp/qk-probe-foo.pid` return a pid.
    exec_stub.responses["cat /tmp/qk-probe-foo.pid"] = (0, "12345\n", "")
    # Health check fast-pass.
    exec_stub.responses["curl -sf"] = (0, "", "")

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    port = runner.start_service("foo")
    assert port == 18900  # first allocation
    # Second call returns the same port (idempotent).
    assert runner.start_service("foo") == 18900
    # Teardown invokes kill.
    runner.teardown_services()
    kill_calls = [c for c in exec_stub.call_args_list if "kill 12345" in " ".join(c.args[1])]
    assert kill_calls, "teardown should kill the captured pid"


def test_runner_start_service_handles_failure_cleanly():
    """When the start command fails, runner records a placeholder entry
    so probes get a clean ERROR result instead of crashing."""
    exec_stub = _make_exec_in_stub()
    exec_stub.responses["nohup"] = (127, "", "binary not found")

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    port = runner.start_service("missing-svc")
    # Port is still allocated (so probes substituting $PORT_missing-svc don't break),
    # but the service entry has empty pid.
    assert port == 18900
    assert runner._services["missing-svc"].pid == ""
    # Teardown is a no-op for empty-pid services.
    runner.teardown_services()


def test_runner_run_probe_substitutes_ports():
    exec_stub = _make_exec_in_stub()
    exec_stub.responses["18901"] = (0, "ok", "")

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    probe = ManualProbe(
        service="api",
        command="curl http://localhost:$PORT_api/health",
        expected="ok",
    )
    result = runner.run_probe(probe, port_map={"api": 18901})
    assert result.matched is True
    # Verify the substituted command was actually used. Each call's
    # second positional arg is the cmd list; flatten + check.
    flat = []
    for c in exec_stub.call_args_list:
        flat.extend(c.args[1])
    invoked = " ".join(flat)
    assert "18901" in invoked


def test_runner_run_probe_handles_dashes_in_service_name():
    """`$PORT_tanren-mcp` and `$PORT_tanren_mcp` both substitute."""
    exec_stub = _make_exec_in_stub()
    exec_stub.responses["19000"] = (0, "ok", "")

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    probe = ManualProbe(
        service="tanren-mcp",
        command="curl http://localhost:$PORT_tanren_mcp/health",
        expected="ok",
    )
    result = runner.run_probe(probe, port_map={"tanren-mcp": 19000})
    assert result.matched is True


def test_runner_run_probe_captures_exec_exception_as_error_result():
    exec_stub = MagicMock(side_effect=RuntimeError("docker daemon dead"))

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    probe = ManualProbe(command="curl /health", expected="ok")
    result = runner.run_probe(probe)
    assert result.matched is False
    assert "docker daemon dead" in result.error


def test_runner_run_all_probes_starts_each_service_once():
    exec_stub = _make_exec_in_stub()
    exec_stub.responses["cat /tmp/qk-probe-svc.pid"] = (0, "111\n", "")
    exec_stub.responses["curl -sf http://localhost:18900"] = (0, "", "")
    # Probe execution.
    exec_stub.responses["curl http://localhost:18900/a"] = (0, "okA", "")
    exec_stub.responses["curl http://localhost:18900/b"] = (0, "okB", "")

    runner = ManualProbeRunner(handle=object(), exec_in=exec_stub)
    probes = [
        ManualProbe(service="svc", command="curl http://localhost:$PORT_svc/a", expected="okA"),
        ManualProbe(service="svc", command="curl http://localhost:$PORT_svc/b", expected="okB"),
    ]
    results = runner.run_all_probes(probes)
    runner.teardown_services()
    assert all(r.matched for r in results), [r.error for r in results]
    # Service was started exactly once.
    nohup_calls = [c for c in exec_stub.call_args_list if "nohup" in " ".join(c.args[1])]
    assert len(nohup_calls) == 1


def test_runner_context_manager_tears_down():
    exec_stub = _make_exec_in_stub()
    exec_stub.responses["cat /tmp/qk-probe-svc.pid"] = (0, "777\n", "")

    with ManualProbeRunner(handle=object(), exec_in=exec_stub) as runner:
        runner.start_service("svc")
    # After exit, kill should have been invoked.
    assert any("kill 777" in " ".join(c.args[1]) for c in exec_stub.call_args_list)


# ----- rendering -------------------------------------------------------------


def test_render_probe_block_empty_returns_empty_string():
    assert render_probe_block([]) == ""


def test_render_probe_block_includes_results():
    p = ManualProbe(command="curl /health", expected="ok", description="health")
    r = ProbeResult(probe=p, rc=0, stdout="ok\n", stderr="", duration_s=0.05, matched=True)
    block = render_probe_block([r])
    assert "MANUAL_PROBE_RESULTS" in block
    assert "MATCHED" in block
    assert "curl /health" in block


def test_render_probe_block_truncates_long_output():
    p = ManualProbe(command="curl big", expected="x")
    long_out = "x" * 5000
    r = ProbeResult(probe=p, rc=0, stdout=long_out, stderr="", duration_s=0.05, matched=True)
    block = render_probe_block([r])
    assert "[truncated]" in block
    # Heuristic: well under the original 5000-char body.
    assert len(block) < 3000


# ----- worker integration -----------------------------------------------------


def test_worker_run_manual_probes_returns_block_when_evidence_present(monkeypatch, tmp_path):
    """The TaskWorker._run_manual_probes path collects manual evidence,
    runs probes via the runner, and returns a non-empty rendered block."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    node = Node(
        id="T-1",
        kind="behavior",
        milestone="M-1",
        title="t",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(
            {"kind": "manual", "command": "curl /health", "expected": "ok", "description": "h"},
        ),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("T-1")

    dag = DAG(nodes={"T-1": node}, milestones={}, raw={})

    worker = TaskWorker(cfg, dag, store, node)
    # Pretend the container handle exists; the runner exec_in is patched
    # at the module level so the actual handle is irrelevant.
    worker.handle = make_handle("T-1")

    # Patch the manual_probe.exec_in injection point. Worker uses
    # `manual_probe.ManualProbeRunner(... exec_in=exec_in)` — to substitute
    # at the call site we patch the worker's view of `exec_in`.
    fake = MagicMock(return_value=(0, "ok", ""))
    monkeypatch.setattr("quikode.worker.exec_in", fake)

    block = worker._run_manual_probes()
    assert block, "worker should emit a manual-probe block when evidence exists"
    assert "MANUAL_PROBE_RESULTS" in block
    assert "MATCHED" in block

    store.conn.close()


def test_worker_run_manual_probes_returns_empty_when_no_evidence(tmp_path):
    """Nodes without manual evidence yield an empty block — `_check`
    falls back to today's no-probe behavior."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    node = Node(
        id="T-2",
        kind="behavior",
        milestone="M-1",
        title="t",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=({"kind": "test"},),  # not manual
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("T-2")
    dag = DAG(nodes={"T-2": node}, milestones={}, raw={})
    worker = TaskWorker(cfg, dag, store, node)
    worker.handle = make_handle("T-2")

    block = worker._run_manual_probes()
    assert block == ""
    store.conn.close()


def test_worker_run_manual_probes_skips_when_no_handle(tmp_path):
    """No container handle (e.g. a degraded path) → empty block, no crash."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    node = Node(
        id="T-3",
        kind="behavior",
        milestone="M-1",
        title="t",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=({"kind": "manual", "command": "curl"},),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("T-3")
    dag = DAG(nodes={"T-3": node}, milestones={}, raw={})
    worker = TaskWorker(cfg, dag, store, node)
    # worker.handle is None by default — exercise the no-handle skip path.
    assert worker.handle is None

    block = worker._run_manual_probes()
    assert block == ""
    store.conn.close()
