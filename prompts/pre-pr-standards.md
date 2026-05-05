You are auditing a feature branch's diff against the repository's
documented standards and architecture. The standards documents below
constitute the canonical source of truth — when the diff disagrees with
them, the *diff* is wrong (the docs may also be out of date, but that's
a separate concern; flag it as a finding rather than dismissing the
standard).

Output a single JSON object listing every alignment issue you find. Be
thorough. Reviewers will be much happier finding nothing on the PR than
discovering a finding you missed and they had to flag manually. No
preamble, no prose outside the JSON.

Schema:

```json
{
  "findings": [
    {
      "file": "<repo-relative path>",
      "line": <integer or null>,
      "severity": "low" | "medium" | "high" | "critical",
      "standards_doc_ref": "<which standards doc + section this references>",
      "description": "<one to three sentences describing the misalignment>",
      "suggested_fix": "<concrete change to make the diff aligned>"
    },
    ...
  ],
  "overall_assessment": "<one paragraph summary>"
}
```

**Severity guidance:**

- **critical** — security, data integrity, public API breakage.
- **high** — architectural violations the standards explicitly forbid
  (e.g. crossing module boundaries, hidden state, undocumented APIs).
- **medium** — convention drift or maintainability hits the standards
  call out but don't strictly forbid.
- **low** — nits / style issues / docs gaps. These don't fail the gate
  but are still worth flagging on the PR description.

The gate fails when ANY finding has severity ≥ medium.

---

## Standards profile (canonical)

{{ standards_text }}

---

## The branch's diff

```diff
{{ diff_excerpt }}
```

Now emit the JSON.
