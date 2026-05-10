# Plan 50 — fix orphan Config fields not plumbed in config_loader

## Why

`Config` (in `quikode/config.py`) declares 118 fields. `config_loader.load_config()`
explicitly enumerates which TOML keys to read for each field. Five fields
exist on Config but have NO read in load_config — so any TOML override
of these is silently ignored, and the cfg object falls back to the field's
default.

Worse: `_log_int_overrides` independently inspects raw TOML keys vs.
defaults and logs `config[X] = Y (overrides Field default Z)` for any TOML
key that mentions a Config field name. It does this without checking
whether the key actually got wired into the returned Config — so the
audit log misleadingly claims overrides took effect when they didn't.

This was discovered 2026-05-10 14:14 trying to bump
`subtask_same_signature_block_count` 5→10 to soften a stop-loss cascade.
Daemon log said "config[subtask_same_signature_block_count] = 10"; cfg
object actually had 5; stop-loss kept tripping at 5.

The five orphans:

| Field | Type | Default | Workspace TOML | Actual |
|---|---|---|---|---|
| `subtask_same_signature_block_count` | int | 5 | 10 | 5 |
| `subtask_witness_timeout_seconds` | int | 15 | 180 | 15 |
| `fixup_planner_timeout_s` | int | (default) | n/a in workspace | default |
| `fixup_planner_retries_on_transient` | int | (default) | n/a in workspace | default |
| `pre_pr_architecture_model` | str | "gpt-5.5" | "gpt-5.5" | matches default by accident |

`subtask_witness_timeout_seconds` is the most operationally significant —
tanren's BDD-heavy run sets 180s but the runtime cap is silently 15s.
That alone would cause a long tail of false-negative behavior witnesses
on slow tests.

## What ships

### Fix the orphans

Add five lines to `quikode/config_loader.py` in the `return Config(...)`
construction, matching the existing pattern:

```python
subtask_same_signature_block_count=int(
    raw.get(
        "subtask_same_signature_block_count",
        defaults.subtask_same_signature_block_count,
    )
),
subtask_witness_timeout_seconds=int(
    raw.get(
        "subtask_witness_timeout_seconds",
        defaults.subtask_witness_timeout_seconds,
    )
),
fixup_planner_timeout_s=int(
    raw.get("fixup_planner_timeout_s", defaults.fixup_planner_timeout_s)
),
fixup_planner_retries_on_transient=int(
    raw.get(
        "fixup_planner_retries_on_transient",
        defaults.fixup_planner_retries_on_transient,
    )
),
pre_pr_architecture_model=str(
    raw.get("pre_pr_architecture_model", defaults.pre_pr_architecture_model)
),
```

### Tighten `_log_int_overrides` against future drift

The audit log should NOT be able to claim overrides that don't actually
take effect. Two-part fix:

1. After constructing the final Config in `load_config()`, compare each
   logged override to the resulting Config field. Log a `WARNING` (not
   INFO) if a TOML key mentioned a Config field but the resulting cfg
   value matches the default — which means the loader silently swallowed
   the override. This makes future orphans loud at runtime.
2. Add a one-shot audit call in `load_config()` that iterates Config's
   model_fields, finds any field whose value in the resulting cfg
   matches `defaults` AND whose name appears as a top-level TOML key in
   raw, and emits a WARNING. The check is cheap and runs once per
   daemon start.

(Alternative: make load_config use Config.model_validate(raw) directly
and stop hand-enumerating fields. That's a bigger refactor and would
break the explicit-fail-on-retired-keys pattern. Out of scope for this
plan; queue as plan 51 candidate.)

### Tests

- New test in `tests/test_config_loader.py` (or wherever): TOML with a
  non-default value for each of the 5 orphan fields → resulting Config
  reflects the override (not the default).
- New test: TOML with `subtask_same_signature_block_count = 99` →
  cfg.subtask_same_signature_block_count == 99.
- New test for the audit: TOML with a stale unwired field name (e.g.
  `subtask_pretend_field = 42`) → no warning if it's not on Config; if
  it IS on Config but unwired, the audit fires a WARNING.
- Update any existing test that asserted defaults for these fields.

### Plans index

Add plan 50 row to `plans/00-INDEX.md`.

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon stop + daemon start (the cfg overrides become live).
4. Observe whether the 180s witness cap unblocks any BDD-heavy tasks
   that were previously failing on 15s timeouts.

## Out of scope

- Refactor load_config to use Config.model_validate directly (plan 51).
- Audit any other places where audit logs make claims about state
  without verifying the resulting object reflects them.
