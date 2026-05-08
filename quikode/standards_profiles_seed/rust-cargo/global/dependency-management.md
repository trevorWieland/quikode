---
kind: standard
name: dependency-management
category: global
importance: high
applies_to:
  - "**/Cargo.toml"
  - "**/Cargo.lock"
applies_to_languages:
  - rust
applies_to_domains:
  - dependencies
---

# Dependency Management

Crates pin versions explicitly and avoid path overrides in published
crates. The lockfile is committed; updates run via `cargo update -p`
and land in their own commits.

## Rules

- Pin versions in `Cargo.toml`; no wildcards.
- No `path = "..."` overrides in published crates.
- Commit `Cargo.lock`; review version bumps in dedicated commits.
