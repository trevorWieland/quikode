---
kind: standard
name: three-tier-test-structure
category: testing
importance: high
applies_to:
  - "**/*.test.ts"
  - "**/*.test.tsx"
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - testing
---

# Three-Tier Test Structure

Tests partition into unit (pure logic), component (rendered UI), and
e2e (full user journeys). Each tier targets a distinct concern: unit
tests validate logic; component tests validate rendering and
interaction; e2e tests validate the full journey across the app shell.

## Rules

- Unit tests use Vitest/Jest; no DOM rendering.
- Component tests use Testing Library + jsdom.
- E2e tests drive the real app shell (Playwright/Cypress).
