---
kind: standard
name: accessibility-enforcement
category: react
importance: high
applies_to:
  - "**/*.tsx"
applies_to_languages:
  - typescript
applies_to_domains:
  - react
  - accessibility
---

# Accessibility Enforcement

`eslint-plugin-jsx-a11y` warnings are configured as errors. Every
interactive element has accessible labels; every image has alt text or
explicit `alt=""` for decoration.

## Rules

- `jsx-a11y/*` rules are errors, not warnings.
- All buttons/links carry accessible names.
- Color is not the sole carrier of meaning.
