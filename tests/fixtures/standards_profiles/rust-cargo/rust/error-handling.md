---
kind: standard
name: error-handling
category: rust
importance: high
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - errors
---

# Error Handling

Library crates use `thiserror`; binaries use `anyhow`.

## Rules

- No `unwrap()` / `panic!()` in production code.
- Wrap errors with context at boundaries.

## Notes

Cross-references the `anyhow` and `thiserror` upstream docs.
