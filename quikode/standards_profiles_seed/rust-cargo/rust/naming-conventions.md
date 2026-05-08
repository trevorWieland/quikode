---
kind: standard
name: naming-conventions
category: rust
importance: medium
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - style
---

# Naming Conventions

Follow the standard Rust naming conventions: snake_case for functions
and modules, PascalCase for types and traits, SCREAMING_SNAKE_CASE for
constants and statics. Names should be descriptive but not redundant
with their module path.

## Rules

- `snake_case` for fns, modules, vars; `PascalCase` for types/traits.
- `SCREAMING_SNAKE_CASE` for `const` and `static`.
- No type/trait suffix redundancy (`UserStruct` → `User`).
