---
kind: standard
name: explicit-return-types
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

# Explicit Return Types

Exported functions declare their return types explicitly. Inferred
return types on public API surface make refactors silently shift the
contract; explicit return types make breakage compile-time-visible.

## Rules

- All exported functions have explicit return type annotations.
- Internal helpers may rely on inference.
- Hooks declare both their return tuple/object shape and dependencies.
