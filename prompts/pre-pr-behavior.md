You are verifying that a feature branch actually delivers the behaviors
its spec claims. The spec lists `expected_evidence` items — for each one,
**actually exercise** the cited interface or run the named witness, and
record what you observed.

You have shell access to the workspace via the dev container. Use it to:

- Run the test suite or a focused test case for the cited behavior.
- Invoke the CLI with the example arguments and confirm the output.
- Curl an HTTP endpoint, query a database, etc.

Do NOT just read the diff and reason about whether the code "looks
right." That's the rubric stage's job. Your job is *empirical
verification*.

Output a single JSON object. No prose outside the JSON.

Schema:

```json
{
  "behaviors": [
    {
      "behavior_id": "<id from expected_evidence>",
      "verified": true | false,
      "evidence_seen": "<concrete observation: command run, output, test that passed>",
      "gap_explanation": "<for verified=false: why couldn't you confirm it; what's missing>"
    },
    ...
  ],
  "overall_assessment": "<one paragraph summary>"
}
```

**verified=true** requires that you ran something and observed the
expected outcome — not "the code reads correctly." When you can't
confirm a behavior despite trying (test missing, interface not
implemented, etc.) mark it `verified=false` with a clear
`gap_explanation`. The fixup planner reads `gap_explanation` to plan
follow-up subtasks, so be specific and actionable.

---

## Spec context

```
{{ plan_text }}
```

## Expected evidence (behaviors to verify)

{% for ev in expected_evidence %}- **{{ ev.kind }}**{% if ev.get('behavior_id') %} `{{ ev.behavior_id }}`{% endif %}: {{ ev.description }}
{% if ev.get('interfaces') %}  - interfaces: {{ ev.interfaces }}
{% endif %}{% if ev.get('witnesses') %}  - witnesses: {{ ev.witnesses }}
{% endif %}{% endfor %}

## The branch's diff

```diff
{{ diff_excerpt }}
```

Now perform the verification and emit the JSON.
