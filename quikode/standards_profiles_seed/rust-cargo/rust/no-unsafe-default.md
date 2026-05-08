---
kind: standard
name: no-unsafe-default
category: rust
importance: critical
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - safety
---

# No Unsafe by Default

`unsafe` blocks require explicit justification and a `// SAFETY:` comment
documenting the invariants the caller upholds. Crates default to
`#![forbid(unsafe_code)]`; opt-out is local and reviewed.

## Rules

- `#![forbid(unsafe_code)]` in every crate by default.
- Each `unsafe` block has a `// SAFETY: ...` justification.
- Unsafe surface is documented in the crate's README / module docs.
