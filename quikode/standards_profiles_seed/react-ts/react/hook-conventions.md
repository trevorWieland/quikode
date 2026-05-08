---
kind: standard
name: hook-conventions
category: react
importance: high
applies_to:
  - "**/*.tsx"
  - "**/*.ts"
applies_to_languages:
  - typescript
applies_to_domains:
  - react
---

# Hook Conventions

Custom hooks are prefixed `use`; the rules-of-hooks lint must clear at
zero warnings. Hooks called conditionally or inside loops are rejected
at the gate.

## Rules

- Custom hook names start with `use`.
- `react-hooks/rules-of-hooks` and `react-hooks/exhaustive-deps` are errors.
- Effects declare exhaustive deps; non-deps are stabilized via ref.
