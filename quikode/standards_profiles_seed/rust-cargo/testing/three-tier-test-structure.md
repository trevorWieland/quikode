---
kind: standard
name: three-tier-test-structure
category: testing
importance: high
applies_to:
  - "**/tests/**/*.rs"
  - "**/src/**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - testing
---

# Three-Tier Test Structure

Tests partition into unit (in-module), integration (`tests/`), and BDD
(scenarios driving real interfaces). Each tier covers a distinct layer:
unit for pure logic, integration for crate boundaries, BDD for
user-observable behavior.

## Rules

- Unit tests live alongside code (`#[cfg(test)] mod tests`).
- Integration tests live in `tests/` per crate.
- BDD scenarios drive the public interface; no in-process shortcuts.
