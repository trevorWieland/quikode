---
kind: standard
name: file-and-function-limits
category: rust
importance: medium
applies_to:
  - "**/*.rs"
applies_to_languages:
  - rust
applies_to_domains:
  - architecture
---

# File and Function Limits

Files cap at ~500 lines; functions cap at modest cyclomatic complexity.
Modules that exceed the cap split along clear boundaries (private
helpers vs. public API, IO vs. pure logic). Function complexity is
managed by extracting named helpers, not by inline comments.

## Rules

- 500-line soft cap per `.rs` file; refactor instead of bloating.
- Cyclomatic complexity ceiling enforced via clippy.
- Extract helpers; do not inline-comment past the cap.
