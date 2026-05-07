"""Manual-probe runner.

V3-005 carryover: tanren's `expected_evidence.kind == "manual"` items
describe curl-style probes against running services (`tanren-mcp`,
`tanren-cli`, etc.). Pre-v3 the checker prompt mentioned them but had
no infrastructure to actually run them — no port allocation, no
credential injection, no background-start.

This module gives the worker a generic runner so the checker prompt
receives objective `MANUAL_PROBE_RESULTS` evidence alongside `just ci`
output. The agent still judges intent; we just make the probes feasible.

Defensive: if probe parsing fails (malformed `expected_evidence`), the
runner logs a warning and skips that probe. The worker keeps running —
manual probes are evidence-augmenting, not blocking.

Contract between `expected_evidence` items and `ManualProbe`:

  Structured form (preferred). Any of these shapes are accepted:
    {"kind": "manual", "service": "tanren-mcp", "command": "curl ...",
     "expected": "ok", "description": "..."}

  Free-text form (fallback). When only `description` is provided the
  parser tries to extract a command (`curl ...`) and an expected
  substring (e.g. `"ok"`) using regex heuristics. If extraction fails
  the probe is skipped with a logged warning — never crashes.

The runner always operates inside the existing execution sandbox via the
backend exec interface, so no new mounts/networks are needed. Services
are background-started with `nohup` and a per-service health check
loop; `teardown_services()` `kill`s them via the captured pids.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("quikode.manual_probe")


class ManualProbe(BaseModel):
    """One probe declared by an `expected_evidence` item."""

    model_config = ConfigDict(frozen=True)

    description: str = Field(
        default="",
        description="Human-readable description, copied from the evidence item.",
    )
    service: str = Field(
        default="",
        description=(
            "Name of the service to start before running the probe. Empty "
            "string means no service start needed (the command is fully "
            "self-contained)."
        ),
    )
    command: str = Field(
        description=(
            "Shell command to run inside the container. May include "
            "$PORT_<service> placeholders that are substituted with the "
            "allocated port for that service."
        ),
    )
    expected: str = Field(
        default="",
        description=(
            "Substring or regex the probe output must contain for the probe "
            "to be considered 'matched'. Empty string means any non-empty "
            "rc=0 output passes."
        ),
    )
    expected_is_regex: bool = Field(
        default=False,
        description="Treat `expected` as a regex instead of a substring.",
    )


class ProbeResult(BaseModel):
    """Result of one manual-probe execution."""

    model_config = ConfigDict(frozen=True)

    probe: ManualProbe
    rc: int
    stdout: str
    stderr: str
    duration_s: float
    matched: bool = Field(
        description=(
            "True when rc == 0 AND (expected is empty OR expected was found in stdout). False otherwise."
        ),
    )
    error: str = Field(
        default="",
        description="Non-empty when the probe failed to run at all (e.g. service start failed).",
    )

    def render_block(self) -> str:
        """Render this result as a single block suitable for inclusion in
        the checker prompt under MANUAL_PROBE_RESULTS."""
        status = "MATCHED" if self.matched else "MISMATCHED"
        if self.error:
            status = f"ERROR ({self.error})"
        out = self.stdout.strip()
        if len(out) > 1500:
            out = out[:1500] + "\n... [truncated]"
        return (
            f"- probe: {self.probe.description or self.probe.command}\n"
            f"  service: {self.probe.service or '(none)'}\n"
            f"  command: {self.probe.command}\n"
            f"  expected: {self.probe.expected or '(any non-empty rc=0)'}\n"
            f"  status: {status} (rc={self.rc}, took {self.duration_s:.2f}s)\n"
            f"  output: |\n    " + out.replace("\n", "\n    ")
        )


# Regex used to fish a curl command out of free-text `description` strings.
_CURL_RE = re.compile(r"(curl\s+[^\n;`]+)", re.IGNORECASE)
# Used to fish an `expected substring` out of the same: looks for either
# `expected: "..."`, `must contain "..."`, or `returns "..."`.
_EXPECTED_RE = re.compile(
    r"(?:expected|must contain|returns|response)\s*[:=]?\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def parse_evidence_to_probe(item: object) -> ManualProbe | None:
    """Parse one `expected_evidence` item into a `ManualProbe`.

    Returns None when the item isn't a manual probe or is malformed beyond
    recovery. NEVER raises — caller can rely on `None` to mean "skip this
    item silently after a warning."
    """
    if not isinstance(item, dict):
        log.warning("manual-probe parse: item is not a dict; skipping (%s)", type(item))
        return None
    data = cast(dict[str, Any], item)
    kind = str(data.get("kind", "")).strip().lower()
    if kind != "manual":
        return None

    description = str(data.get("description", "")).strip()
    service = str(data.get("service", "")).strip()
    command = str(data.get("command", "")).strip()
    expected = str(data.get("expected", "")).strip()
    is_regex = bool(data.get("expected_is_regex", False))

    if not command and description:
        # Free-text fallback: try to fish a curl command out of the description.
        m = _CURL_RE.search(description)
        if m:
            command = m.group(1).strip()
        m2 = _EXPECTED_RE.search(description)
        if m2 and not expected:
            expected = m2.group(1).strip()

    if not command:
        log.warning(
            "manual-probe parse: no command in evidence item (description=%r); skipping",
            description[:120],
        )
        return None

    return ManualProbe(
        description=description,
        service=service,
        command=command,
        expected=expected,
        expected_is_regex=is_regex,
    )


# ----- runtime ----------------------------------------------------------------


class ContainerExec(Protocol):
    """Anything that can `exec_in(handle, cmd, ..., timeout=...)` — i.e.
    `quikode.execution.exec_in` in production, a stub in tests."""

    def __call__(
        self,
        handle: Any,
        cmd: list[str],
        log_path: Any = None,
        stdin: str | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]: ...


@dataclass
class _StartedService:
    name: str
    port: int
    pid: str  # captured via $! → string from container shell
    health_command: str | None
    log_file: str  # path inside the container


@dataclass
class ManualProbeRunner:
    """Runs `ManualProbe`s inside the dev container.

    Keeps minimal state: a port allocator, a started-services list, and a
    credentials map. `start_service` tracks what's been started so
    `teardown_services` can kill them.

    Designed to be the worker's helper — instantiate per `_check()` call,
    invoke `run_all_probes`, then `teardown_services()` (or use as a
    context manager).
    """

    handle: Any  # the execution sandbox; opaque to the runner
    exec_in: ContainerExec
    log_path: Any = None
    credentials: dict[str, str] = field(default_factory=dict)
    binary_lookup: dict[str, str] = field(default_factory=dict)
    health_check_timeout_s: int = 30
    probe_timeout_s: int = 60
    _next_port: int = 18900
    _services: dict[str, _StartedService] = field(default_factory=dict)

    def __enter__(self) -> ManualProbeRunner:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.teardown_services()

    # --- ports ---------------------------------------------------------------

    def _allocate_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

    # --- services ------------------------------------------------------------

    def start_service(self, name: str) -> int:
        """Spin up a service binary in the background inside the container.

        Returns the assigned port. Tries `target/release/<binary>` first
        (fast path — assumes the workspace was built); falls back to a
        diagnostic message recorded in the runner state so probes can
        report the failure cleanly without the worker crashing.

        Idempotent: a second call for the same service name returns the
        already-allocated port.
        """
        if name in self._services:
            return self._services[name].port

        port = self._allocate_port()
        binary = self.binary_lookup.get(name, f"target/release/{name}")
        log_file = f"/tmp/qk-probe-{name}.log"

        # Build env-var injection for the credentials. Keys are exported as
        # KEY=VALUE on the same shell line so the binary inherits them.
        env_prefix = self._render_env_prefix(name)
        port_env = f"PORT={port} "

        # nohup + disown via `&`; capture pid into a file so teardown can read it.
        pid_file = f"/tmp/qk-probe-{name}.pid"
        cmd = (
            f"cd /workspace && {env_prefix}{port_env}nohup {binary} > {log_file} 2>&1 & echo $! > {pid_file}"
        )
        rc, _out, err = self.exec_in(
            self.handle,
            ["bash", "-lc", cmd],
            log_path=self.log_path,
            timeout=30,
        )
        if rc != 0:
            log.warning(
                "manual-probe: failed to start service %s (rc=%d): %s",
                name,
                rc,
                err.strip()[:200],
            )
            # Stash a placeholder so probes referring to this service get a
            # clean ERROR result instead of a silent miss.
            self._services[name] = _StartedService(
                name=name,
                port=port,
                pid="",
                health_command=None,
                log_file=log_file,
            )
            return port

        # Read the pid back.
        rc, pid_out, _err = self.exec_in(
            self.handle,
            ["bash", "-lc", f"cat {pid_file}"],
            log_path=self.log_path,
            timeout=10,
        )
        pid = pid_out.strip() if rc == 0 else ""

        health_cmd = f"curl -sf http://localhost:{port}/health"
        self._services[name] = _StartedService(
            name=name,
            port=port,
            pid=pid,
            health_command=health_cmd,
            log_file=log_file,
        )
        # Best-effort wait for /health (or any TCP open). Tolerates a
        # service without /health: we still wait the full window before
        # returning, but probes can run regardless of the health verdict.
        self._await_health(name)
        return port

    def _render_env_prefix(self, service: str) -> str:
        """Render KEY=VALUE pairs to prepend to the service start line.

        Pulls credentials from `self.credentials` — caller is responsible
        for populating it (e.g. from `cfg.repo_path/.quikode/secrets.toml`
        or environment variables). The runner deliberately does NOT read
        `os.environ` itself: hidden environment leakage into containers is
        a footgun. Caller injects exactly what it wants visible.
        """
        # The simplest path: every credential key is exported for every
        # service. If a future need arises to scope keys per-service, add
        # a `{service: [keys]}` map here.
        if not self.credentials:
            return ""
        parts = [f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in self.credentials.items()]
        return " ".join(parts) + " " if parts else ""

    def _await_health(self, name: str) -> None:
        svc = self._services.get(name)
        if svc is None or not svc.pid or svc.health_command is None:
            return
        deadline = time.time() + self.health_check_timeout_s
        while time.time() < deadline:
            rc, _out, _err = self.exec_in(
                self.handle,
                ["bash", "-lc", svc.health_command],
                log_path=self.log_path,
                timeout=5,
            )
            if rc == 0:
                return
            time.sleep(1.0)
        log.info(
            "manual-probe: service %s health check did not pass within %ds; running probes anyway",
            name,
            self.health_check_timeout_s,
        )

    def teardown_services(self) -> None:
        for svc in list(self._services.values()):
            if not svc.pid:
                continue
            try:
                self.exec_in(
                    self.handle,
                    ["bash", "-lc", f"kill {svc.pid} 2>/dev/null || true"],
                    log_path=self.log_path,
                    timeout=10,
                )
            except Exception as e:  # teardown must not raise
                log.debug("manual-probe: teardown of %s raised %s", svc.name, e)
        self._services.clear()

    # --- probe execution -----------------------------------------------------

    def _substitute_ports(self, command: str, port_map: dict[str, int]) -> str:
        """Replace `$PORT_<service>` placeholders in `command`.

        Also accepts `${PORT_<service>}`. Unknown placeholders are left
        intact so the caller can see the problem in the probe output.
        """
        out = command
        for name, port in port_map.items():
            for key in (f"$PORT_{name}", f"${{PORT_{name}}}"):
                out = out.replace(key, str(port))
            # Also support upper/lower variants (`tanren-mcp` → `tanren_mcp` etc).
            sanitized = name.replace("-", "_")
            for key in (f"$PORT_{sanitized}", f"${{PORT_{sanitized}}}"):
                out = out.replace(key, str(port))
        return out

    def run_probe(self, probe: ManualProbe, port_map: dict[str, int] | None = None) -> ProbeResult:
        port_map = dict(port_map or {})
        # Auto-include ports for any started services.
        for name, svc in self._services.items():
            port_map.setdefault(name, svc.port)
        cmd = self._substitute_ports(probe.command, port_map)

        # Prepare credential env so the probe itself sees them too (e.g.
        # curl with -H "x-api-key: $TANREN_MCP_API_KEY").
        env_prefix = self._render_env_prefix(probe.service or "")
        full = f"{env_prefix}{cmd}"
        start = time.time()
        try:
            rc, out, err = self.exec_in(
                self.handle,
                ["bash", "-lc", full],
                log_path=self.log_path,
                timeout=self.probe_timeout_s,
            )
        except Exception as e:  # turn into ERROR result, never crash the caller
            return ProbeResult(
                probe=probe,
                rc=-1,
                stdout="",
                stderr=str(e),
                duration_s=time.time() - start,
                matched=False,
                error=f"probe execution raised: {e}",
            )
        duration = time.time() - start

        matched = self._classify(rc, out, probe)
        return ProbeResult(
            probe=probe,
            rc=rc,
            stdout=out,
            stderr=err,
            duration_s=duration,
            matched=matched,
            error="",
        )

    @staticmethod
    def _classify(rc: int, stdout: str, probe: ManualProbe) -> bool:
        if rc != 0:
            return False
        if not probe.expected:
            return bool(stdout.strip())
        if probe.expected_is_regex:
            try:
                return bool(re.search(probe.expected, stdout))
            except re.error as e:
                log.warning(
                    "manual-probe: invalid regex %r (%s); falling back to substring match",
                    probe.expected,
                    e,
                )
                return probe.expected in stdout
        return probe.expected in stdout

    def run_all_probes(self, probes: list[ManualProbe]) -> list[ProbeResult]:
        """Start any required services, run each probe, and tear down.

        Caller MUST handle the lifecycle either by using the runner as a
        context manager OR by calling `teardown_services()` afterwards.
        This method is the convenience entry point for most workers.
        """
        # 1. Start every distinct service exactly once.
        port_map: dict[str, int] = {}
        for p in probes:
            if p.service and p.service not in port_map:
                port_map[p.service] = self.start_service(p.service)
        # 2. Run each probe.
        results: list[ProbeResult] = []
        for p in probes:
            results.append(self.run_probe(p, port_map=port_map))
        return results


# ----- helper that the worker calls ------------------------------------------


def collect_probes_from_evidence(evidence: list[dict] | tuple[dict, ...]) -> list[ManualProbe]:
    """Parse a node's `expected_evidence` list into a list of `ManualProbe`s.

    Items that aren't manual probes are silently dropped. Malformed
    items log a warning and are skipped — the worker continues without
    them. NEVER raises.
    """
    out: list[ManualProbe] = []
    for item in evidence or ():
        try:
            probe = parse_evidence_to_probe(item if isinstance(item, dict) else {})
        except Exception as e:  # defensive — caller must never see a parse failure
            log.warning("manual-probe: parse_evidence_to_probe raised on %r: %s", item, e)
            continue
        if probe is not None:
            out.append(probe)
    return out


def render_probe_block(results: list[ProbeResult]) -> str:
    """Render a list of probe results as a single MANUAL_PROBE_RESULTS block
    suitable for inclusion in the checker prompt. Empty `results` returns
    an empty string."""
    if not results:
        return ""
    body = "\n".join(r.render_block() for r in results)
    return f"## MANUAL_PROBE_RESULTS\n\n{body}\n"


def credentials_from_env(keys: list[str]) -> dict[str, str]:
    """Best-effort: pull `keys` from `os.environ` and return a dict.

    Empty / unset keys are dropped. Caller is responsible for choosing
    which keys to expose to the container — the runner deliberately
    does not exfiltrate the full environment."""
    out: dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            out[k] = v
    return out
