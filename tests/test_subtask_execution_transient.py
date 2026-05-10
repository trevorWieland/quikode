"""Phase 1A: objective subtask check classifies container-vanished as transient.

The 2026-05-07 incident burned 12 tasks to BLOCKED at the 50-attempt hard
ceiling because `_run_subtask_check_command` returned `transient=False` even
when the failure was a vanished dev container ("No such container",
"container is not running", rc=137). With `transient=False` each iteration
incremented the attempt counter; the gate failed in <1s; ceiling reached in
~60s of pure infrastructure noise. These tests lock in the fix:

- transient=True for "No such container" stderr + rc=1
- transient=True for "container ... is not running" stderr + rc=1
- transient=True for "Error response from daemon" stderr + rc=1
- transient=True for rc=137 (OOM kill) regardless of stderr
- transient=False for genuine compile/lint failures
"""

from __future__ import annotations

from quikode.agents.transient_quota import (
    _is_transient_agent_auth_failure,
    _is_transient_container_failure,
)


def test_no_such_container_is_transient():
    stderr = "Error response from daemon: No such container: qk-r-0008-6ff621-dev"
    assert _is_transient_container_failure(1, stderr) is True


def test_container_not_running_is_transient():
    stderr = "Error response from daemon: container abc123def is not running"
    assert _is_transient_container_failure(1, stderr) is True


def test_daemon_error_is_transient():
    stderr = "Error response from daemon: connection refused"
    assert _is_transient_container_failure(1, stderr) is True


def test_rc_137_is_transient_regardless_of_stderr():
    """OOM kill is always transient — see _is_transient_container_failure docstring."""
    assert _is_transient_container_failure(137, "") is True
    assert _is_transient_container_failure(137, "anything") is True


def test_rc_zero_is_never_transient():
    assert _is_transient_container_failure(0, "") is False
    assert _is_transient_container_failure(0, "Error response from daemon") is False


def test_real_compile_failure_is_not_transient():
    stderr = "error[E0599]: no method named `foo` found in scope"
    assert _is_transient_container_failure(101, stderr) is False


def test_test_failure_is_not_transient():
    stderr = "thread 'main' panicked at 'index out of bounds'"
    assert _is_transient_container_failure(101, stderr) is False


def test_empty_stderr_with_nonzero_rc_is_not_transient():
    # No marker → treat as a real failure. Caller's responsibility to
    # decide whether to retry based on the verdict.
    assert _is_transient_container_failure(1, "") is False


def test_codex_refresh_token_reuse_is_transient_agent_auth_failure():
    stderr = (
        "ERROR: Your access token could not be refreshed because your refresh token was already used. "
        '{"code":"refresh_token_reused"}'
    )
    assert _is_transient_agent_auth_failure(99, "", stderr) is True


def test_bare_unauthorized_is_not_transient_agent_auth_failure():
    assert _is_transient_agent_auth_failure(1, "", "HTTP error: 401 Unauthorized") is False
