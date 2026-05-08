# Plan 38 - model capacity, subscription windows, failover chains, and graceful pausing

## Goal

Use coding model subscriptions and API-key billing intentionally across all projects.

The system must track real usage windows where possible, estimate safely where unavoidable, enforce configured minimum reserves, fail over through role-specific CLI/model chains, and pause affected agent roles when no configured option is available.

## Requirements

- Track real usage remaining for window-based subscriptions such as ChatGPT Pro OAuth Codex usage.
- Track request volume, token volume, duration, cost, and quota signals by CLI/model/role/project.
- Support chains such as:

```toml
[models.roles.planner]
chain = [
  { cli = "codex", model = "gpt-5.5", account = "chatgpt-pro", min_remaining = "20%" },
  { cli = "opencode", model = "openai/gpt-5.5", account = "zen-api-key" },
]
```

- Fail down when the preferred option is unavailable.
- Automatically swap back up the chain when the preferred option becomes available.
- If a role exhausts its chain, pause work requiring that role, notify, and resume when capacity returns.
- Sigterm or sigkill downstream agent processes that exceed configured budget policy.

## Current state

- Agent role config is one `AgentRole(cli, model, extra_args)`.
- Agent calls record tokens, duration, and cost when parsable.
- Plan 19A added internal retry on quota signals, but it retries inside the agent layer and hides capacity from the scheduler.
- There is no global subscription/window model.

## Design

Add:

- `ModelAccount`: provider/account/window metadata.
- `ModelWindow`: start/end, limit, observed usage, reserved floor, confidence.
- `ModelOption`: CLI+model+account+extra args.
- `ModelChain`: ordered options per agent role.
- `ModelCapacityManager`: reconciles live signals and answers availability.

Availability states:

- `available`: safe to schedule.
- `reserved`: below configured reserve; only emergency phases may use.
- `cooldown`: quota/rate limit observed; retry after known or estimated time.
- `unknown`: no signal; policy decides whether to allow.
- `unavailable`: chain option cannot run.

## Real usage tracking

Use provider-specific adapters:

- Codex OAuth: parse CLI quota/rate-limit output and any available local usage/status command.
- opencode/API-key: record API reported usage/cost where available.
- Claude: parse subscription window/quota output if configured later.

Do not pretend tokens equal subscription budget. Store both:

- `observed_tokens`
- `observed_requests`
- `observed_duration_s`
- `observed_cost_usd`
- `provider_window_remaining` when the provider exposes it
- `confidence = provider_reported | inferred | unknown`

## Budget enforcement

Add per-option policies:

- minimum remaining percentage or absolute units
- max request duration
- max role spend per hour/day
- kill behavior: `sigterm_after_s`, `sigkill_after_s`
- protected roles or emergency override flags

The process supervisor must be able to terminate the exact agent process tree for a running call and mark the attempt as budget-killed, not as checker/doer failure.

## Implementation

1. Replace single `AgentRole` config with `AgentRolePolicy(chain=[...])`; keep loader migration explicit and temporary.
2. Add `model_capacity.py` with account/window/chain models.
3. Add control-store tables:
   - `model_accounts`
   - `model_windows`
   - `model_usage_events`
   - `model_capacity_decisions`
4. Move quota handling out of "sleep internally for hours" and into scheduler-visible capacity states.
5. Teach agent builder to receive a resolved `ModelOption`, not a role config.
6. Add process-tree cancellation for over-budget calls.
7. Add notification when a role is paused due to exhausted chain.
8. Add auto-resume when a capacity adapter reports availability again.

## Acceptance

- Planner can fail over from `codex:gpt-5.5` to `opencode:gpt-5.5` when the Codex account falls below reserve.
- Checker work can continue while planner work is paused if checker has available chain options.
- When every doer option is unavailable, doer candidates remain queued with `paused_model_capacity` reason and the control plane sends one notification.
- When capacity recovers, queued work resumes without manual retry.
- TUI/API can show per role: selected model, fallback reason, remaining window, request rate, token rate, cost rate, and paused candidates.

