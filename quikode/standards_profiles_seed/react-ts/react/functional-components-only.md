---
kind: standard
name: functional-components-only
category: react
importance: high
applies_to:
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - react
---

# Functional Components Only

New components are functional with hooks; no class components in new
code. Existing class components are migrated when touched, not in a
big bang. State machines beyond hook capability indicate a refactor
to a state-machine library, not a return to classes.

## Rules

- All new components are function components.
- No `extends React.Component` in new files.
- Class-component migrations land alongside the touching feature.
