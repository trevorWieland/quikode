---
kind: standard
name: mock-boundaries
category: testing
importance: medium
applies_to:
  - "**/*.test.ts"
  - "**/*.test.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - testing
---

# Mock Boundaries

Mock at module / network boundaries — never inside a tested component.
MSW for HTTP; explicit dependency injection for service classes;
`vi.mock` only for cross-module isolation.

## Rules

- HTTP mocks via MSW; assert request/response shape.
- No `vi.mock` of the system under test's own module.
- Test data factories live alongside the test file.
