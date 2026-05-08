---
kind: standard
name: no-test-skipping
category: testing
importance: critical
applies_to:
  - "**/*.test.ts"
  - "**/*.test.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - testing
---

# No Test Skipping

`describe.skip`, `it.skip`, `test.skip`, and `xdescribe` / `xit` are
forbidden in committed code. A failing test is fixed or removed; a
skipped test silently rots and erodes the gate's meaning.

## Rules

- No `.skip` / `x*` test variants in committed code.
- No early-return-from-test that bypasses assertions.
- Flaky tests are quarantined in a tracked issue, not silenced in-tree.
