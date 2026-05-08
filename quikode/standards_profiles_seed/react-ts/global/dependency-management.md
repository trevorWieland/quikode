---
kind: standard
name: dependency-management
category: global
importance: high
applies_to:
  - "package.json"
  - "pnpm-lock.yaml"
applies_to_languages:
  - typescript
applies_to_domains:
  - dependencies
---

# Dependency Management

Use pnpm; the lockfile is committed; major version bumps land in
their own commits. Caret ranges (`^x.y.z`) for minor/patch only;
exact pins for majors and security-sensitive packages.

## Rules

- pnpm only; commit `pnpm-lock.yaml`.
- No `^` on major version bumps; pin majors explicitly.
- `pnpm audit` must clear at gate severity.
