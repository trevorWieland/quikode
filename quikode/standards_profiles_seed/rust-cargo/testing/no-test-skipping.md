---
kind: standard
name: no-test-skipping
category: testing
importance: critical
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - testing
---

# No Test Skipping

`#[ignore]`, `skip!()`, and equivalent test-skip mechanisms are
forbidden in committed code. A failing test is fixed or removed; a
skipped test silently rots and erodes the gate's meaning.

## Rules

- No `#[ignore]` in committed code.
- No `return Ok(())` early-exits that bypass assertions.
- Flaky tests are quarantined in a tracked issue, not silenced in-tree.
