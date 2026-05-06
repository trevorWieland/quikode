# Plan 02 — exponential backoff on rate-limit and network errors

## Problem

Every `gh`/`git`/`gh api graphql` call uses `subprocess.run(timeout=60, check=False)`,
emits a warning on rc!=0, then the next worker tick retries the same call immediately.
There is **no backoff anywhere**. The two failure shapes that bite hardest:

1. **GitHub rate-limit (HTTP 429 from gh api).** Review-watcher polls every ~10s.
   When the secondary rate-limit lights up, every poll hits 429 for ~60 seconds. With
   7 in-flight tasks all polling, we burn the rate budget faster every cycle.
2. **`git push` network blip.** The `pushing` and `rebasing_to_main` states attempt one
   push, transient failure → BLOCKED. A 2s SSH hiccup costs a manual `qk retry`.

## Hot spots (file:line)

- `quikode/github.py:106` — `gh pr view`
- `quikode/github.py:163` — `gh pr checks` / `gh run`
- `quikode/github_graphql.py:148` — `gh api graphql` for review threads
- `quikode/github_graphql.py:242` — `gh api` thread resolve
- `quikode/workers/subtask_completion.py` — `git push`
- `quikode/workers/rebase_branch.py:179`, `rebase_conflicts.py:118` — force-push
- `quikode/workers/pr_lifecycle.py` — `gh pr edit`, `gh pr create`, `gh pr comment`

## Approach (state-machine-respecting, no tanren-side changes)

Add a single `quikode/net_retry.py` helper:

```python
def run_with_backoff(
    cmd: list[str], *, retries: int = 3, base_delay_s: float = 2.0,
    classify: Callable[[CompletedProcess], Literal["ok", "transient", "hard"]],
    cwd: Path | None = None, timeout: int = 60, input: str | None = None,
) -> CompletedProcess: ...
```

`classify` decides whether to retry. Default classifier recognises:
- 429 / "rate limit" / "secondary rate" in stderr → transient (wait double)
- "could not resolve host" / "connection reset" / TLS timeout → transient
- "Resource not accessible" / "Not Found" / 401 / 403 → hard (don't retry)
- rc 0 → ok

Backoff schedule: 2s, 4s, 8s. Cap at 3 retries — anything more belongs in the
state-machine retry budget, not silently inside one call.

Wire it into the four hot files above. Two-line replacement per call site:
```python
proc = subprocess.run(cmd, ...)  # was
proc = net_retry.run_with_backoff(cmd, classify=net_retry.gh_classifier, ...)
```

## Why this is safe under "wipe rather than carry poisoned work"

Backoff doesn't change task progress. It only delays ack-of-failure. The state
machine still moves to BLOCKED if `run_with_backoff` ultimately fails — same
terminal state, just gated on real failure, not transient blips.

## Tests

- Stub `subprocess.run`, return 429 once + 0 second time → verify two calls.
- Return 429 four times → verify three retries, last call observed, BLOCKED.
- Return "Not Found" → verify single call, no retries.

## Out-of-scope

- Adaptive rate-limit window (read `X-RateLimit-Reset` header). Future work; gh CLI
  doesn't surface that header trivially. The blunt 2/4/8 is enough for now.
- Cross-task coordination (one task's 429 should pause sibling polls). Real fix is a
  process-wide GH API throttler. Plan separately.
