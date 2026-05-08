---
kind: standard
name: strict-linting-gate
category: global
importance: high
applies_to:
  - "**/*.ts"
  - "**/*.tsx"
  - "package.json"
applies_to_languages:
  - typescript
applies_to_domains:
  - build
  - ci
---

# Strict Linting Gate

ESLint and `tsc --noEmit` must pass on every commit. Warnings are
errors at the gate; ignored rules require an in-file justification
referencing a tracked issue.

## Rules

- `eslint --max-warnings=0` in CI.
- `tsc --noEmit` runs as a gate step.
- Disabled rules carry a `// eslint-disable-next-line ... -- reason` comment.
