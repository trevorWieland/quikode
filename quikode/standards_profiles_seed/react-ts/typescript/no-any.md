---
kind: standard
name: no-any
category: typescript
importance: critical
applies_to:
  - "**/*.ts"
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - typing
---

# No `any`

`any` is forbidden in committed code. Use `unknown` and narrow with
type guards, or define a proper discriminated union. `any` defeats
the purpose of TypeScript and propagates silently into call sites.

## Rules

- ESLint rule `@typescript-eslint/no-explicit-any` is `"error"`.
- Use `unknown` + narrowing instead.
- External-data parsing goes through a validator (zod / valibot).
