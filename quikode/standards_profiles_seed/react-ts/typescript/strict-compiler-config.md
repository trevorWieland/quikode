---
kind: standard
name: strict-compiler-config
category: typescript
importance: critical
applies_to:
  - "tsconfig.json"
  - "**/*.ts"
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - typing
---

# Strict Compiler Config

`tsconfig.json` enables `strict: true` plus `noUncheckedIndexedAccess`
and `exactOptionalPropertyTypes`. Loosening any flag requires an
in-repo justification in `tsconfig.json` itself.

## Rules

- `strict: true` (covers strictNullChecks, noImplicitAny, etc.).
- `noUncheckedIndexedAccess: true`.
- `exactOptionalPropertyTypes: true`.
