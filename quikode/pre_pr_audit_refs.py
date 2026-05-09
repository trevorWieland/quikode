"""Plan 35 PR-B: shared helpers for the standards + architecture audits.

Extracted from `pre_pr_audit.py` to keep that module under the 600-line
architecture-budget cap while the audit pipeline grew from 4 stages to
5. Two concerns live here:

1. `CitedSection` + `_render_cited_sections` — render a list of pinned
   doc-section bodies into the audit prompt's `*_refs_in_diff` block.
   Plan 35 §3.5: the auditor reads the same cited prose the doer/checker
   saw via `ec_targeted`.
2. `collect_standards_refs_in_diff` / `collect_architecture_refs_in_diff`
   — resolve `(doc_path, section)` planner citations against the
   contract's loaded corpora into `CitedSection` triples for prompt
   rendering.

All functions return plain `list[dict]` findings + standard tuples; the
audit-orchestrator side (`pre_pr_audit.run_standards_audit`,
`run_architecture_audit`) bridges into `StageOutcome`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .evaluation_contract import EvaluationContract


@dataclass(frozen=True)
class CitedSection:
    """One cited doc-section with body inlined for the audit prompt."""

    doc_path: str
    section: str
    body: str
    title: str = ""


def render_cited_sections(sections: list[CitedSection], *, char_cap: int = 30_000) -> str:
    """Render `(doc, section, body)` triples for the audit prompt's
    `*_refs_in_diff` block. Plan 35 §3.5: cited section bodies are
    inlined so the auditor reads the same prose the doer/checker saw."""
    if not sections:
        return "(no refs cited by this task's subtasks)"
    lines: list[str] = []
    for cs in sections:
        header_extra = f" — {cs.title}" if cs.title else ""
        lines.append(f"### `{cs.doc_path}` § {cs.section}{header_extra}")
        lines.append("")
        lines.append("```")
        lines.append(cs.body[:6000])
        lines.append("```")
        lines.append("")
    text = "\n".join(lines).strip("\n")
    if len(text) > char_cap:
        return text[:char_cap].rstrip() + "\n[CITED SECTIONS TRUNCATED]"
    return text


def _section_match(doc_sections: tuple[str, ...], section: str) -> bool:
    target = " ".join(section.split()).lower()
    if not target:
        return False
    return any(" ".join(h.split()).lower() == target for h in doc_sections)


def collect_standards_refs_in_diff(
    *,
    contract: EvaluationContract,
    cited: list[tuple[str, str]],
) -> tuple[list[CitedSection], set[str]]:
    """Walk the plan's `standards_referenced` citations and return
    `(rendered_cited_sections, cited_doc_paths_set)`. Resolves bodies
    against the contract's loaded profile corpus; missing refs are
    silently skipped (validators reject them earlier)."""
    sections: list[CitedSection] = []
    paths: set[str] = set()
    for doc_path, section in cited:
        paths.add(doc_path)
        for profile in contract.standards.profiles:
            for doc in profile.docs:
                if doc.repo_relative != doc_path:
                    continue
                if _section_match(doc.sections, section):
                    sections.append(
                        CitedSection(
                            doc_path=doc_path,
                            section=section,
                            body=doc.body,
                            title=f"profile: {profile.name}; importance: {doc.importance}",
                        )
                    )
                break
    return sections, paths


def collect_architecture_refs_in_diff(
    *,
    contract: EvaluationContract,
    cited: list[tuple[str, str]],
) -> tuple[list[CitedSection], set[str]]:
    """Walk the plan's `architecture_referenced` citations and return
    `(rendered_cited_sections, cited_doc_paths_set)`. Resolves bodies
    against the contract's loaded architecture corpus; missing refs are
    silently skipped (validators reject them earlier)."""
    sections: list[CitedSection] = []
    paths: set[str] = set()
    for doc_path, section in cited:
        paths.add(doc_path)
        for doc in contract.architecture.corpus.docs:
            if doc.repo_relative != doc_path:
                continue
            if _section_match(doc.sections, section):
                sections.append(
                    CitedSection(
                        doc_path=doc_path,
                        section=section,
                        body=doc.body,
                        title=doc.title,
                    )
                )
            break
    return sections, paths


__all__ = [
    "CitedSection",
    "collect_architecture_refs_in_diff",
    "collect_standards_refs_in_diff",
    "render_cited_sections",
]
