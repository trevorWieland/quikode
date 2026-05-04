You are reviewing one inline review thread on a pull request created by an
AI coding agent. Decide whether the reviewer's comment is **CORRECT** (a
legitimate code change is needed), **INCORRECT** (the reviewer is wrong or
the concern doesn't apply to the actual change), or **NEEDS_DISCUSSION**
(the comment raises a question that needs human input — e.g. an open design
choice, ambiguity in spec interpretation).

Respond with a single JSON object — no preamble, no explanation outside the
JSON. Schema:

```json
{
  "verdict": "correct" | "incorrect" | "needs_discussion",
  "rationale": "<one sentence; will be logged>",
  "reply": "<polite reply text to post on the thread; ONLY for incorrect verdict, else empty>"
}
```

Decision criteria:

- **correct**: the comment identifies a real issue that should be fixed in
  the code. The fix is plausible (e.g. "this method ignores the error
  return", "this string isn't escaped", "missing test for the empty case").
  Pass through to the fixup planner.
- **incorrect**: the comment is mistaken — it's based on a misreading of
  the diff, references code that doesn't exist, or asks for something the
  spec explicitly excludes. The `reply` field MUST contain a short
  professional response explaining *why* we're not changing this. Don't
  argue or hedge — be direct and reference the spec or the diff if
  relevant. Examples:
    - "This file is intentionally unchanged in this slice; the spec
      reserves <X> for a later subtask."
    - "The function does handle the error case at line 47; happy to add
      a test if helpful but the logic is correct as-is."
- **needs_discussion**: the comment raises a real point but the answer
  depends on a design decision the spec doesn't pin down. Don't auto-reply;
  leave for human triage.

Bias toward **correct** when uncertain — the cost of fixing a thing that
didn't need fixing is small; the cost of dismissing a real bug is higher.
But INCORRECT verdicts are valuable: they let the system make forward
progress without burning a planner round on a non-issue.

---

## Spec context

PR scope (the spec being implemented):

```
{{ plan_text or "(no plan text on file)" }}
```

## The review thread

- File / line: `{{ thread_path }}:{{ thread_line if thread_line is not none else "?" }}`
- Reviewer: `{{ thread_author }}` (bot: `{{ thread_is_bot }}`)
- Body:

```
{{ thread_body }}
```

{% if recent_diff_excerpt %}
## Recent diff at the cited file (last commit, first 80 lines)

```
{{ recent_diff_excerpt }}
```
{% endif %}

Now emit the JSON.
