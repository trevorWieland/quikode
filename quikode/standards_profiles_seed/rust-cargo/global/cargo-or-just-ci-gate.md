---
kind: standard
name: cargo-or-just-ci-gate
category: global
importance: high
applies_to:
  - "**/*.rs"
  - "**/Cargo.toml"
  - "justfile"
applies_to_languages:
  - rust
applies_to_domains:
  - build
  - ci
---

# Cargo / Just CI Gate

A single CI command must encapsulate all checks (compile, lint, fmt,
test, audit). The command must exit `rc=0` on a clean repo, and any
failure anywhere is the gate's failure.

## Rules

- One canonical command (typically `just ci` or `cargo ci`).
- Exit code 0 on success; non-zero on any check failure.
- No interactive prompts; deterministic output.
