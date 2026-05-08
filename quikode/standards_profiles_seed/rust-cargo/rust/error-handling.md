---
kind: standard
name: error-handling
category: rust
importance: critical
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - errors
---

# Error Handling

Library crates use `thiserror` for typed errors that callers can match
on; binaries use `anyhow` for ergonomic error chaining. Panics, unwraps,
and `expect` are reserved for invariants the type system cannot
express.

## Rules

- `thiserror::Error` in libraries; `anyhow::Result` in binaries.
- No `unwrap()` / `expect()` / `panic!()` in production paths.
- Wrap errors with context (`.with_context(...)`) at boundaries.
