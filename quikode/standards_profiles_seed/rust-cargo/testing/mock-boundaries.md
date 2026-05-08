---
kind: standard
name: mock-boundaries
category: testing
importance: medium
applies_to:
  - "**/tests/**/*.rs"
  - "**/src/**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - testing
---

# Mock Boundaries

Mock at crate boundaries — never within a crate. Within-crate mocking
hides real bugs; cross-crate mocking lets you test in isolation while
still exercising the real production interface to dependencies.

## Rules

- Mocks live at trait/crate boundaries, not at internal function calls.
- Use traits for mocked dependencies; concrete types for real ones.
- Integration tests prefer real implementations where feasible.
