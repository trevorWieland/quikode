# Tanren Profile

Use `--profile tanren` for the Tanren workspace.

Profile-owned defaults:

- image: `quikode-tanren-dev:latest`
- base branch: `main`
- local CI: `just ci`
- subtask check: `just check`
- pre-commit runner: `auto`
- database: per-task Postgres sidecar with DB `tanren`
- merge policy: squash merge with branch deletion

## Resources (current run)

WSL on a 24c/256GB Threadripper, `.wslconfig` set to 200GB / 36 processors:

- `max_parallel = 12` — capped by coding-agent subscription limits, not host capacity (host can sustain 16).
- `cpu_per_task = 2`, `mem_per_task_gb = 10` — covers heavy-tail audit-stage memory peaks (~7.5GB on R-0006-class tasks). Drop to 8 only if confident no audit-stage spikes; bump to 12 if OOM kills appear.
- `host_reserved_cpu = 4`, `host_reserved_mem_gb = 16` — leaves the Windows host headroom for desktop / browser / light gaming.

For dedicated cloud (e.g. Hetzner CCX63, 48c/192GB): same `max_parallel=12` is safe; could push to 16 with `mem_per_task_gb=8` if no OOM kills observed AND the subscription tier supports the higher concurrency.

## Stacking + review-ready signal (plan 30)

```toml
[stacking]
strategy = "aggressive"   # full cross-milestone chaining
readiness = "settled"     # 15-min gate after AWAITING_REVIEW

review_ready_settle_s = 900
notify_ntfy_url = "https://ntfy.sh"
notify_ntfy_topic = "<secret topic>"
```

Children only fork once their parent has been in `awaiting_review` for ≥ 15 min — guaranteeing a CI-green base, plus giving the operator a window to read incoming context comments before downstream work commits to the parent's diff. The same threshold fires the ntfy push to the operator's phone.

`stacking_strategy = "aggressive"` enables cross-milestone chaining: a child in M-0003 can stack on an M-0001 parent. The plan-30 settle gate makes this safe — every cross-milestone dependent starts from a CI-green base regardless of milestone boundary.

## BDD Conventions

Tanren BDD feature files live under `tests/bdd/features`. Behavior-proof tag validation is part of `just check` and `just ci`.

When a task touches behavior evidence, the plan should include BDD subtasks late in the subtask order, after the implementation surfaces they witness exist.

## Validation

Useful commands inside the task container:

```bash
just check
just ci
```

Targeted BDD diagnosis:

```bash
just check-bdd-tags
```

## Archived Branches

Old failed Tanren work can be inspected through:

```bash
quikode archive show <id>
quikode archive branch <id>
```

Archived branches are references only. Fresh strict reruns are the default.
