---
kind: standard
name: discriminated-unions
category: typescript
importance: medium
applies_to:
  - "**/*.ts"
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - typing
---

# Discriminated Unions

Prefer tagged unions (`{ kind: "a" } | { kind: "b" }`) over class
inheritance for variant types. Tagged unions interact correctly with
exhaustiveness checking; inheritance hides the variant set behind
runtime polymorphism.

## Rules

- Use a `kind: "..."` tag on every union variant.
- Exhaustiveness via `assertNever(x: never)` in default branches.
- No class inheritance for variant modelling.
