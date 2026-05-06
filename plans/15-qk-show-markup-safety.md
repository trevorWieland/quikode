# Plan 15 — `qk show` artifact rendering must not interpret content as Rich markup

## Why this plan exists

`qk show <task>` is the primary way the operator (and the supervising agent)
reads the latest triage / checker / doer / planner output for a task. It crashed
with a `rich.markup.MarkupError` during the very first status sweep of an
overnight run:

```
MarkupError: closing tag '[/workspace/crates/tanren-app-services/src/lib.rs:206]'
at position 283 doesn't match any open tag
```

Root cause: `quikode/cli_show_export.py:_print_show_artifacts` was calling
`console.print(body)` on raw artifact content. Artifact bodies routinely contain
substrings that look like Rich tags — most commonly bracketed file paths
(`[/workspace/path/to/file.rs:206]`) emitted by `cargo`, `rustc`, BDD harnesses,
and our own checker output. Rich treats `[/...]` as a *closing* tag, fails to
match it against an open tag, and aborts the whole `console.print` call.

When this fires, the operator can't read the failing task at all. The whole
"soft-cap audit" loop in `orientation.md` depends on `qk show <id>` being
trustworthy — without it the agent can't categorize a soft-cap breach, can't
decide what to fix, can't unstick a worker. This is the highest-priority
observability bug uncovered so far.

## What changes

`_print_show_artifacts` now:

1. Splits the print into two calls.
2. Prints the artifact body with `markup=False, highlight=False` so brackets,
   ANSI colors, and other content are rendered verbatim.
3. Prints the truncation suffix (`... (N more chars; pass --full)`) separately,
   keeping the dim-grey styling intact.

That's the entire change — six edited lines in `cli_show_export.py`. No new
behavior, no API change. The existing surrounding output (`-- {kind} --`
header, kind dedup, ordering by `ts DESC`) is untouched.

## Why no broader sweep

Only one site in `cli_show_export.py` prints arbitrary artifact content
(`console.print(body)` on line 522 of the old file). Every other `console.print`
uses *our own* format strings or values from controlled sources (timestamps,
counts, enum values, table cells). Those are safe to keep with markup enabled —
disabling markup there would lose the bold/cyan/green styling that makes the
output skimmable.

If a future site starts dumping arbitrary subprocess output through `console`,
it will need the same treatment. The narrow fix here matches the principle of
*"don't add abstractions beyond what the task requires"* — there's no signal
yet that we need a generalized "render-untrusted-text" helper.

## Validation

- Running `qk show R-0021` no longer crashes — verified after reinstall during
  the same monitoring cycle.
- Existing `qk show` output for tasks with simple text artifacts is byte-equal
  except for the dim-suffix being on its own line (an unintentional but
  acceptable cosmetic delta — the suffix used to be glued to the truncated
  body).
- Validation ladder: ruff/format/ty/pytest all pass.

## Status

**Shipped** in this commit on `optimizations`.
