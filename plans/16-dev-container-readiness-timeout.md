# Plan 16 — `wait_dev_ready` timeout was too tight for cold cluster startup

## What happened

First overnight cycle after a hard daemon restart with `--max-parallel 16`. The
orchestrator brought up 16 dev + 16 postgres containers in rapid succession.
Sixteen tasks then transitioned `provisioning -> failed` with:

```
dev container qk-r-XXXX-XXXXXX-dev not ready within 60s
```

The error fires from `quikode/docker_env.py:wait_dev_ready`, which polls
`docker exec ... test -f /tmp/qk-ready` every 500ms. The dev image's entrypoint
touches `/tmp/qk-ready` as its last step — after copying agent auth files
(`.claude.json`, codex creds, opencode creds) into the container. The probe was
called from `task_worker.py` with `timeout_s=60`.

## Why 60s was wrong

After the failures, I sampled the failed containers directly:

```
$ docker exec qk-r-0002-3a65a1-dev ls -la /tmp/qk-ready
-rw-r--r-- 1 dev dev 0 May  6 17:11 /tmp/qk-ready
```

Every sampled container *did* finish its entrypoint — just past the 60s
deadline. The mtime sat right around `failure_ts + 30..120s`. Root cause:
when 16 containers boot at once on a cold cluster, the auth-file copy step
contends for I/O on the bind-mounted `~/.config/...` paths. Single-container
boot is well under 60s; 16-way parallel boot is not.

The cost asymmetry matters. The probe is `docker exec test -f` every 500ms —
it returns immediately when the marker appears. So a longer ceiling costs
nothing on the happy path. But a too-tight ceiling **wrongly fails live,
working containers** and (worse) leaves them running, holding the entire
16×10GB resource budget. The orchestrator then can only schedule a handful of
new slots until the zombies are reaped, badly throttling forward progress.

## Cluster-bug pattern

Per `orientation.md`'s troubleshooting matrix:

> **7+ tasks all failing on same root cause within minutes** → cluster bug — fix
> the shared cause and reinstall once for all.

Sixteen-on-one-root-cause inside two minutes is the textbook signature.

## What changes

Two files, three lines of behavior:

1. `quikode/docker_env.py:wait_dev_ready` — default `timeout_s=30` → `120`.
   Other callers that pass no override now get a more generous ceiling.
2. `quikode/workers/task_worker.py` — call-site override `timeout_s=60` →
   `timeout_s=240`. The worker is the cold-cluster path; it should tolerate a
   full minute or two of I/O contention on top of the typical entrypoint cost.
3. Comment at the call site explains the asymmetry so future maintainers don't
   re-tighten it without measuring.

This intentionally does **not**:

- Add config knobs for the timeout. There's no signal yet that operators need
  per-host tuning. If we ever do, that's a separate, narrow plan.
- Stagger or batch the container starts. The simpler ceiling-bump fixes the
  observed bug; staggering is a much larger change with its own risks (head-of-
  line blocking on the slowest container's start). Revisit only if a longer
  timeout still proves insufficient on slower cloud hosts.
- Auto-recover containers whose readiness probe timed out (i.e. don't auto-
  resume a `provisioning -> failed` transition). The current FAILED state is
  still useful as a signal — the fix here just makes it a much rarer event.

## Operational follow-up for the in-flight run

The 16 containers from this incident are still running but orphaned (the daemon
moved on to other tasks). Operator action this cycle:

1. `docker rm -f` the 16 stale `qk-r-*-dev` and matching `qk-r-*-pg` pairs.
2. `qk resume` each of the 16 affected tasks; the worker will provision fresh
   containers using the new 240s ceiling.

These steps are run-specific cleanup, not part of the code shipment.

## Validation

- `uv run ruff check quikode tests` — clean.
- `uv run ruff format --check quikode tests` — clean.
- `uv run ty check quikode tests` — clean.
- `uv run pytest tests/ -q` — clean.
- Functional verification: after reinstall + daemon restart, the 5 in-flight
  tasks (R-0003, R-0015, R-0016, R-0040, R-0041) reach `doing_subtask` without
  tripping the new ceiling, and the resumed 16 tasks provision cleanly.

## Status

**Shipped** in this commit on `optimizations`.
