"""Plan 33 + Plan 35: hard validators on planner output.

Validators run against every parsed `Plan` / `FixupPlan` before the
planner's output is accepted. The spec-planner driver runs all five:

* `validate_rubric_coverage(plan, contract)` — every category in the
  contract's rubric appears in at least one subtask's `rubric_targets`.
  **Spec plans only.** A fixup plan addresses specific audit findings,
  not whole rubric categories — so the fixup driver replaces this with
  `validate_finding_coverage` instead (Plan 33 calibration follow-up).
* `validate_evidence_partition(plan, node)` — every id in
  `node.expected_evidence` appears in **exactly one** subtask's
  `behavior_evidence_advanced` (not zero, not more than one). Applies
  to both spec and fixup plans (fixups can only narrow the partition,
  not widen it; the audit's `behavior:<id>` findings need exactly-once
  coverage).
* `validate_standards_refs(plan, contract)` — Plan 35: every cited
  `standards_referenced` entry resolves to a doc under a loaded
  standards profile (frontmatter `kind: standard`), and the cited
  section heading exists in that doc. Architecture-doc citations in
  `standards_referenced` are rejected with a bucket-correction message.
* `validate_architecture_refs(plan, contract)` — Plan 35 (NEW): every
  cited `architecture_referenced` entry resolves under
  `cfg.architecture_docs_dir`, and the cited section heading exists.
  Standards-profile citations in `architecture_referenced` are rejected
  with a bucket-correction message.
* `validate_finding_coverage(plan, audit_findings)` — fixup-only.
  Every audit finding-id is covered by exactly one subtask's
  stage-typed field, with namespace dispatch (`rubric:<cat>` →
  `rubric_targets`, `standards:<doc§section>` → `standards_referenced`,
  `architecture:<doc§section>` → `architecture_referenced`,
  `behavior:<id>` → `behavior_evidence_advanced`).

On failure, callers re-prompt the planner with the validator's message
(max 2 re-prompts per Plan 33 D3) before BLOCKing with
`failure_reason="planner_validator_<which>"`. Z-99
(`STABILIZATION_SUBTASK_ID`) is exempt from
`validate_evidence_partition` (it claims no witnesses; all evidence is
partitioned across earlier subtasks).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from .architecture_docs import find_arch_doc, find_arch_section
from .evaluation_contract import EvaluationContract, _evidence_canonical_id
from .standards_profiles import find_doc, find_section
from .subtask_schema import STABILIZATION_SUBTASK_ID, FixupPlan, Plan

if TYPE_CHECKING:
    from .dag import Node


class PlannerValidationError(ValueError):
    """Raised when a Plan violates one of the Plan 33 validators.

    The `which` attribute identifies the failing validator for the BLOCK
    reason wiring (`failure_reason="planner_validator_<which>"`).
    """

    def __init__(self, which: str, message: str) -> None:
        super().__init__(message)
        self.which = which
        self.message = message


# The rubric source-text format produced by `evaluation_contract._build_rubric`
# starts each category line with `- **<category>**`. Parse those out so the
# validator can match against the planner's `rubric_targets[].category`.
_RUBRIC_LINE_RE = re.compile(r"^\s*-\s+\*\*(?P<cat>[^*]+)\*\*", re.MULTILINE)


def _categories_from_contract(contract: EvaluationContract) -> list[str]:
    """Extract the configured rubric category list out of the contract's
    rubric source text. Stable per-contract."""
    return [m.group("cat").strip() for m in _RUBRIC_LINE_RE.finditer(contract.rubric.source_text)]


def validate_rubric_coverage(plan: Plan, contract: EvaluationContract) -> None:
    """Every rubric category must appear in at least one subtask's
    `rubric_targets`. Z-99 (when injected) covers all categories at the
    min score by construction (Plan 33 D4) — so coverage holds trivially
    when Z-99 is present, but we still validate explicitly so a planner
    that DELETES Z-99 (forbidden) gets caught.
    """
    expected = _categories_from_contract(contract)
    if not expected:
        raise PlannerValidationError(
            "rubric_coverage",
            "the EvaluationContract has no rubric categories; the workspace "
            "config (`pre_pr_rubric_categories`) must list at least one "
            "category before planning can proceed",
        )
    seen: set[str] = set()
    for s in plan.subtasks:
        for tgt in s.rubric_targets:
            seen.add(tgt.category)
    missing = [cat for cat in expected if cat not in seen]
    if missing:
        bullets = "\n".join(
            f"- {cat!r} is not advanced by any subtask; assign it to at least one subtask's `rubric_targets`"
            for cat in missing
        )
        raise PlannerValidationError(
            "rubric_coverage",
            f"validate_rubric_coverage: {len(missing)} rubric category/categories "
            f"have no subtask coverage. Fix:\n{bullets}",
        )
    # Surface unknown categories too — typo-class errors that would slip
    # past coverage but produce noise downstream.
    expected_set = set(expected)
    unknown: list[tuple[str, str]] = []
    for s in plan.subtasks:
        for tgt in s.rubric_targets:
            if tgt.category not in expected_set:
                unknown.append((s.id, tgt.category))
    if unknown:
        bullets = "\n".join(
            f"- subtask {sid!r} references category {cat!r} which is not in the "
            f"workspace's rubric category list ({sorted(expected_set)!r})"
            for sid, cat in unknown
        )
        raise PlannerValidationError(
            "rubric_coverage",
            f"validate_rubric_coverage: subtask(s) reference unknown rubric "
            f"category/categories. Fix:\n{bullets}",
        )


def _evidence_ids_for_node(node: Node) -> list[str]:
    """Mirror `evaluation_contract._evidence_canonical_id` so the planner
    sees the same id namespace the contract renders."""
    return [_evidence_canonical_id(ev) for ev in node.expected_evidence]


def _classify_evidence_claims(
    plan: Plan | FixupPlan, expected: list[str]
) -> tuple[list[str], list[tuple[str, list[str]]], list[tuple[str, str]]]:
    """Partition the plan's behavior_evidence_advanced declarations into
    (missing, duplicated, unknown). Returns three lists ready for the
    error-message assembly. Z-99 (STABILIZATION_SUBTASK_ID) is filtered
    out of holders by D4 (its claim list is empty by construction); a
    planner that puts an id on Z-99 will see it counted toward the
    duplicated/unknown buckets, which is what we want.
    """
    holders: dict[str, list[str]] = defaultdict(list)
    for s in plan.subtasks:
        for evid in s.behavior_evidence_advanced:
            holders[evid].append(s.id)
    expected_set = set(expected)
    missing: list[str] = [evid for evid in expected if evid not in holders]
    duplicated: list[tuple[str, list[str]]] = [
        (evid, sorted(owners)) for evid, owners in holders.items() if evid in expected_set and len(owners) > 1
    ]
    unknown: list[tuple[str, str]] = [
        (owner, evid) for evid, owners in holders.items() if evid not in expected_set for owner in owners
    ]
    return missing, duplicated, unknown


def validate_evidence_partition(plan: Plan | FixupPlan, node: Node) -> None:
    """Every id in `node.expected_evidence` appears in EXACTLY ONE
    subtask's `behavior_evidence_advanced` across the whole plan.

    Z-99 is exempt — by Plan 33 D4 it claims no witnesses; all evidence
    is partitioned across the earlier subtasks.
    """
    expected = _evidence_ids_for_node(node)
    if not expected:
        unexpected_claims = [(s.id, evid) for s in plan.subtasks for evid in s.behavior_evidence_advanced]
        if unexpected_claims:
            bullets = "\n".join(
                f"- subtask {sid!r} claims to advance evidence id {evid!r} "
                f"but the node has no expected_evidence"
                for sid, evid in unexpected_claims
            )
            raise PlannerValidationError(
                "evidence_partition",
                "validate_evidence_partition: this node declares no "
                f"expected_evidence; remove unexpected claim(s):\n{bullets}",
            )
        return

    missing, duplicated, unknown = _classify_evidence_claims(plan, expected)
    # Z-99 filter is implicit: Z-99's behavior_evidence_advanced is () by
    # construction (Plan 33 D4), so it never appears in `holders`.
    _ = STABILIZATION_SUBTASK_ID  # documentation pin
    if not missing and not duplicated and not unknown:
        return

    parts: list[str] = []
    if missing:
        parts.append(
            "missing evidence (no subtask claims to advance it):\n"
            + "\n".join(f"- {evid!r}" for evid in missing)
            + "\nAssign each to exactly one subtask's `behavior_evidence_advanced`."
        )
    if duplicated:
        parts.append(
            "duplicated evidence (multiple subtasks claim to advance the same id):\n"
            + "\n".join(f"- {evid!r}: {owners!r}" for evid, owners in duplicated)
            + "\nThe behavior_evidence_advanced field is a partition; "
            "assign each id to exactly one subtask."
        )
    if unknown:
        parts.append(
            "unknown evidence (subtask claims an id not in node.expected_evidence):\n"
            + "\n".join(f"- subtask {sid!r}: {evid!r}" for sid, evid in unknown)
            + f"\nValid ids: {expected!r}"
        )
    raise PlannerValidationError(
        "evidence_partition",
        "validate_evidence_partition:\n\n" + "\n\n".join(parts),
    )


def validate_gauntlet_strategy(plan: Plan) -> None:
    """Plan 33 §4.3: `gauntlet_strategy` must be 200-2000 chars on real
    planner output. Missing/below-200 → re-prompt. Above-2000 raises here
    (the spec calls for "truncate with WARN" but truncation is properly
    a render-time concern; for a planner-level validator we treat it as a
    re-prompt-able error). Unit-tests construct minimal Plan objects with
    `gauntlet_strategy=""` and never run this validator — it fires only
    on the parsed-from-agent path in `subtasks.py`.
    """
    s = plan.gauntlet_strategy or ""
    n = len(s)
    if n < 200:
        raise PlannerValidationError(
            "gauntlet_strategy",
            f"validate_gauntlet_strategy: `gauntlet_strategy` is "
            f"{n} chars (need >= 200). Write a 200-2000 char section "
            f"explaining how the plan passes each of the four audit "
            f"stages on cycle 1 (which subtasks carry rubric weight, "
            f"how standards alignment is preserved, where witnesses "
            f"come from, what local-CI risks Z-99 mops up).",
        )
    if n > 2000:
        raise PlannerValidationError(
            "gauntlet_strategy",
            f"validate_gauntlet_strategy: `gauntlet_strategy` is "
            f"{n} chars (need <= 2000). Tighten the prose.",
        )


def validate_standards_refs(plan: Plan | FixupPlan, contract: EvaluationContract) -> None:
    """Plan 35: every `standards_referenced[].doc_path` must resolve to a
    loaded standards-profile doc (frontmatter `kind: standard`), and the
    cited `section` must be a heading present in that doc.

    Replaces the pre-Plan-35 `validate_standards_paths`. Architecture
    docs cited in `standards_referenced` get a bucket-correction message
    pointing the planner at `architecture_referenced`.
    """
    profiles = contract.standards.profiles
    profile_names = [p.name for p in profiles]
    bad: list[tuple[str, str, str]] = []
    for s in plan.subtasks:
        for ref in s.standards_referenced:
            doc = find_doc(profiles, ref.doc_path)
            if doc is None:
                bad.append(
                    (
                        s.id,
                        ref.doc_path,
                        (
                            "doc_path does not live under any configured "
                            f"standards profile (loaded: {profile_names!r}). "
                            "Standards refs MUST cite profile docs (e.g. "
                            "profiles/rust-cargo/rust/error-handling.md), "
                            "not architecture or feature documentation. If "
                            "you meant to cite a project-architecture doc, "
                            "use `architecture_referenced` instead."
                        ),
                    )
                )
                continue
            # Per Plan 35 §2.7: confirm the matched doc is a profile-kind
            # standard. The loader requires `kind: standard` in frontmatter
            # but we re-check here defensively.
            # (The loader stores no `kind` field on the dataclass; profiles
            # only contain `kind: standard` docs by construction. This is
            # an invariant assertion shaped into a friendly error.)
            if not find_section(doc, ref.section):
                available = ", ".join(doc.sections) or "(no headings parsed)"
                bad.append(
                    (
                        s.id,
                        f"{ref.doc_path}§{ref.section}",
                        f"section heading {ref.section!r} not found in doc; available: {available}",
                    )
                )
    if bad:
        bullets = "\n".join(
            f"- subtask {sid!r} cites standards ref {p!r}: {reason}" for sid, p, reason in bad
        )
        raise PlannerValidationError(
            "standards_refs",
            f"validate_standards_refs: {len(bad)} standards reference(s) "
            f"do not resolve to a loaded profile doc + section. Fix:\n{bullets}",
        )


def validate_architecture_refs(plan: Plan | FixupPlan, contract: EvaluationContract) -> None:
    """Plan 35: every `architecture_referenced[].doc_path` must resolve
    under `cfg.architecture_docs_dir` (the loaded ArchitectureCorpus),
    and the cited `section` must be a heading present in that doc.

    Standards-profile docs cited in `architecture_referenced` get a
    bucket-correction message pointing the planner at
    `standards_referenced`.
    """
    corpus = contract.architecture.corpus
    bad: list[tuple[str, str, str]] = []
    for s in plan.subtasks:
        for ref in s.architecture_referenced:
            doc = find_arch_doc(corpus, ref.doc_path)
            if doc is None:
                bad.append(
                    (
                        s.id,
                        ref.doc_path,
                        (
                            "doc_path does not live under the configured "
                            f"architecture_docs_dir ({corpus.root}). "
                            "Architecture refs MUST cite project-architecture "
                            "docs, not standards profiles or feature "
                            "documentation. If you meant to cite a "
                            "language/framework standard, use "
                            "`standards_referenced` instead."
                        ),
                    )
                )
                continue
            if not find_arch_section(doc, ref.section):
                available = ", ".join(doc.sections) or "(no headings parsed)"
                bad.append(
                    (
                        s.id,
                        f"{ref.doc_path}§{ref.section}",
                        f"section heading {ref.section!r} not found in doc; available: {available}",
                    )
                )
    if bad:
        bullets = "\n".join(
            f"- subtask {sid!r} cites architecture ref {p!r}: {reason}" for sid, p, reason in bad
        )
        raise PlannerValidationError(
            "architecture_refs",
            f"validate_architecture_refs: {len(bad)} architecture "
            f"reference(s) do not resolve under the configured "
            f"architecture_docs_dir. Fix:\n{bullets}",
        )


def _is_parse_failure_finding(finding_id: str) -> bool:
    """True iff the audit-finding id has the `<stage>:parse_failure`
    shape produced by `pre_pr_audit._parse_failure_outcome`.

    Plan 38 PR-B.5: parse_failure findings are auditor-side
    schema-validation failures (the JsonAgent layer's two-strikes
    pydantic re-prompt path). They are structural, carry no content for
    a fixup subtask to address, and are filtered out of
    `validate_finding_coverage`'s expected set.
    """
    _, _, tail = finding_id.partition(":")
    return tail == "parse_failure"


def _classify_finding_coverage(
    plan: FixupPlan, expected: list[str]
) -> tuple[list[str], list[tuple[str, list[str]]], list[tuple[str, str]]]:
    """Partition the fixup plan's stage-typed coverage into
    (missing, duplicated, unknown) for the audit-finding namespace.

    A finding id is covered when it can be matched to a subtask's
    stage-typed field by namespace prefix:

    * `rubric:<category>` → subtask's `rubric_targets[*].category` == category
    * `standards:<doc_path>` or `standards:<doc_path>§<section>` →
      subtask's `standards_referenced[*].doc_path` matches.
    * `behavior:<id>` → subtask's `behavior_evidence_advanced` contains id.

    `plan.findings_addressed` (the plan-level array) is consulted as a
    secondary signal: if the planner declares a finding addressed there,
    we trust it ONLY when at least one subtask owns the matching stage-
    typed coverage. The audit-completeness check stays partition-shaped
    (exactly one subtask) so two subtasks claiming the same finding via
    overlapping coverage surface as `duplicated`.
    """
    holders: dict[str, list[str]] = defaultdict(list)
    for s in plan.subtasks:
        for tgt in s.rubric_targets:
            holders[f"rubric:{tgt.category}"].append(s.id)
        for ref in s.standards_referenced:
            holders[f"standards:{ref.doc_path}"].append(s.id)
            holders[f"standards:{ref.doc_path}§{ref.section}"].append(s.id)
        for arch_ref in s.architecture_referenced:
            holders[f"architecture:{arch_ref.doc_path}"].append(s.id)
            holders[f"architecture:{arch_ref.doc_path}§{arch_ref.section}"].append(s.id)
        for evid in s.behavior_evidence_advanced:
            holders[f"behavior:{evid}"].append(s.id)

    missing: list[str] = []
    duplicated: list[tuple[str, list[str]]] = []
    for fid in expected:
        owners = holders.get(fid, [])
        if not owners:
            missing.append(fid)
        elif len(set(owners)) > 1:
            duplicated.append((fid, sorted(set(owners))))

    expected_set = set(expected)
    unknown: list[tuple[str, str]] = []
    for fid_key, owners in holders.items():
        # Don't flag standards-doc-only keys that we synthesize as a fallback
        # for finding ids that lack an explicit `§section` part.
        if fid_key in expected_set:
            continue
        # Skip the synthetic doc-only standards key — only flag stage-typed
        # claims that don't map to any expected finding when the namespace is
        # rubric: or behavior: (those are unambiguous).
        if fid_key.startswith(("rubric:", "behavior:")):
            for owner in owners:
                unknown.append((owner, fid_key))
    return missing, duplicated, unknown


def validate_finding_coverage(plan: FixupPlan, audit_findings: list[str]) -> None:
    """Plan 33 calibration: fixup-only completeness check.

    Every id in `audit_findings` must be addressed by EXACTLY ONE
    subtask via the stage-typed field matching the finding's namespace
    (`rubric:` → `rubric_targets`; `standards:` → `standards_referenced`;
    `architecture:` → `architecture_referenced`; `behavior:` →
    `behavior_evidence_advanced`). Mirrors the partition discipline of
    `validate_evidence_partition` but scoped to the audit bundle.

    No-op when `audit_findings` is empty (e.g. `fixup-final` /
    `fixup-ci` / `fixup-review` triggers that don't carry a typed
    finding bundle — the trigger context is the failure context).

    Plan 38 PR-B.5 carve-out: `<stage>:parse_failure` findings are
    auditor-side schema-validation failures (the JsonAgent layer
    re-prompted, the second response also failed pydantic). They have
    no content to address — the fixup re-runs the audit, which either
    parses cleanly or fails the same way. We filter them out of the
    expected set so they don't appear as `missing`. The fixup planner
    is told (via prompt) not to allocate subtasks for them.

    Why this exists (vs. `validate_rubric_coverage`):
    `validate_rubric_coverage` insists every rubric **category** be
    advanced. A fixup plan's job is to close specific audit gaps —
    declaring `rubric_targets: []` on a transport/CI fix is correct
    behavior. The fixup driver swaps `validate_rubric_coverage` for
    this validator (Plan 33 calibration follow-up after the tanren
    R-0002 BLOCK).
    """
    if not audit_findings:
        return
    expected = [fid for fid in audit_findings if not _is_parse_failure_finding(fid)]
    if not expected:
        return
    missing, duplicated, unknown = _classify_finding_coverage(plan, expected)
    if not missing and not duplicated and not unknown:
        return

    parts: list[str] = []
    if missing:
        parts.append(
            "missing finding coverage (no subtask's stage-typed field "
            "matches the finding's namespace):\n"
            + "\n".join(f"- {fid!r}" for fid in missing)
            + "\nAssign each finding to exactly one subtask: a "
            "`rubric:<cat>` finding goes into `rubric_targets`; "
            "`standards:<doc_path>[§section]` goes into "
            "`standards_referenced`; "
            "`architecture:<doc_path>[§section]` goes into "
            "`architecture_referenced`; `behavior:<id>` goes into "
            "`behavior_evidence_advanced`."
        )
    if duplicated:
        parts.append(
            "duplicated finding coverage (multiple subtasks claim the "
            "same audit finding):\n"
            + "\n".join(f"- {fid!r}: {owners!r}" for fid, owners in duplicated)
            + "\nThe audit completeness check is a partition; assign "
            "each finding to exactly one subtask."
        )
    if unknown:
        parts.append(
            "unknown stage-typed claim (subtask declares a "
            "rubric/behavior coverage that doesn't match any audit "
            "finding):\n"
            + "\n".join(f"- subtask {sid!r}: {fid!r}" for sid, fid in unknown)
            + f"\nValid finding ids from this audit: {expected!r}"
        )
    raise PlannerValidationError(
        "finding_coverage",
        "validate_finding_coverage:\n\n" + "\n\n".join(parts),
    )
