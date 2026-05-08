"""Common typed primitives shared across modules.

Plan 38 PR-B.7: the retired `AgentResult`, `IntentReviewOutcome`,
`IntentVerdict`, `CheckerOutcome`, and `CriterionVerdict` shapes were
retired alongside the retired `Agent.run` transport. The JsonAgent
layer (`quikode.agents.json_protocol`) carries `JsonAgentResult`
instead; intent-review verdicts flow as `agent_schemas.IntentReviewVerdict`;
worker checker outcomes live in `quikode.workers.outcomes.CheckerOutcome`.

This module retains only the cross-cutting `Verdict` enum used by the
worker outcome dataclasses + checker plumbing.
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    """Outcome of a checker pass on a subtask or whole-spec slice."""

    PASS = "PASS"
    FAIL = "FAIL"
