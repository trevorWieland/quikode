"""Plan 35 PR-B: shared helpers for the standards + architecture audits.

Extracted from `pre_pr_audit.py` to keep that module under the 600-line
architecture-budget cap while the audit pipeline grew from 4 stages to
5. Three concerns live here:

1. `CitedSection` + `_render_cited_sections` — render a list of pinned
   doc-section bodies into the audit prompt's `*_refs_in_diff` block.
   Plan 35 §3.5: the auditor reads the same cited prose the doer/checker
   saw via `ec_targeted`.
2. `changed_files_from_diff` — parse `diff --git a/<path> b/<path>`
   headers from a unified diff, used by the unreferenced-applicable
   detectors.
3. `unreferenced_applicable_standards` / `unreferenced_applicable_architecture`
   — Plan 35 §2.10 detectors that emit a `medium` severity finding when
   the diff touches a file matching a profile doc's `applies_to` glob
   (or `cfg.architecture_path_map`'s key) but no subtask cited the
   mapped doc.
4. `collect_standards_refs_in_diff` / `collect_architecture_refs_in_diff`
   — resolve `(doc_path, section)` planner citations against the
   contract's loaded corpora into `CitedSection` triples for prompt
   rendering.

All functions return plain `list[dict]` findings + standard tuples; the
audit-orchestrator side (`pre_pr_audit.run_standards_audit`,
`run_architecture_audit`) bridges into `StageOutcome`.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
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


def changed_files_from_diff(diff_excerpt: str) -> list[str]:
    """Parse `diff --git a/<path> b/<path>` headers from a unified diff.

    Returns the unique repo-relative paths touched. Used by the
    unreferenced-applicable detectors (§2.10) — we don't need exact line
    coverage, only the set of files the audit should consider.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in diff_excerpt.splitlines():
        if not line.startswith("diff --git "):
            continue
        # `diff --git a/<path> b/<path>` — pull the b-side (rename target).
        parts = line.split(" b/", 1)
        if len(parts) != 2:
            continue
        path = parts[1].strip()
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def unreferenced_applicable_standards(
    *,
    contract: EvaluationContract,
    changed_files: list[str],
    cited_doc_paths: set[str],
) -> list[dict]:
    """Plan 35 §2.10: when the diff touches a file matching a profile
    doc's `applies_to` glob and no subtask cited that doc, emit a
    `unreferenced-applicable-standard` finding (severity medium).
    """
    findings: list[dict] = []
    for profile in contract.standards.profiles:
        for doc in profile.docs:
            if doc.repo_relative in cited_doc_paths:
                continue
            applies = doc.applies_to or ()
            if not applies:
                continue
            matched = [f for f in changed_files if any(fnmatch.fnmatch(f, glob) for glob in applies)]
            if not matched:
                continue
            findings.append(
                {
                    "kind": "unreferenced_applicable_standard",
                    "id": f"unreferenced-applicable-standard-{doc.repo_relative}",
                    "severity": "medium",
                    "profile_doc_ref": doc.repo_relative,
                    "description": (
                        f"the planner did not cite `{doc.repo_relative}` but "
                        f"profile {profile.name!r} declares it `applies_to` "
                        f"glob(s) {list(applies)!r}, and the diff touches: "
                        + ", ".join(matched[:5])
                        + (" …" if len(matched) > 5 else "")
                    ),
                    "concrete_fix": (
                        f"cite `{doc.repo_relative}` from a future subtask's "
                        "`standards_referenced` (with the relevant section), "
                        "or document why this profile doc does not apply to "
                        "the touched files."
                    ),
                    "file": matched[0],
                    "line": None,
                }
            )
    return findings


def unreferenced_applicable_architecture(
    *,
    cfg: Config,
    changed_files: list[str],
    cited_doc_paths: set[str],
) -> list[dict]:
    """Plan 35 §2.10: when the diff touches a path matching a
    `cfg.architecture_path_map` glob and no subtask cited the mapped
    doc, emit `unreferenced-applicable-architecture` (severity medium).
    """
    findings: list[dict] = []
    seen_doc_targets: set[str] = set()
    for path_glob, doc_path in cfg.architecture_path_map.items():
        if doc_path in cited_doc_paths or doc_path in seen_doc_targets:
            continue
        matched = [f for f in changed_files if fnmatch.fnmatch(f, path_glob)]
        if not matched:
            continue
        seen_doc_targets.add(doc_path)
        findings.append(
            {
                "kind": "unreferenced_applicable_architecture",
                "id": f"unreferenced-applicable-architecture-{doc_path}",
                "severity": "medium",
                "architecture_doc_ref": doc_path,
                "description": (
                    f"the planner did not cite `{doc_path}` but "
                    f"`architecture_path_map` maps changed file glob "
                    f"{path_glob!r} to it, and the diff touches: "
                    + ", ".join(matched[:5])
                    + (" …" if len(matched) > 5 else "")
                ),
                "concrete_fix": (
                    f"cite `{doc_path}` from a future fixup-subtask's "
                    "`architecture_referenced` (with the relevant "
                    "section), or document why this architecture doc "
                    "does not apply to the touched files."
                ),
                "file": matched[0],
                "line": None,
            }
        )
    return findings


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
    "changed_files_from_diff",
    "collect_architecture_refs_in_diff",
    "collect_standards_refs_in_diff",
    "render_cited_sections",
    "unreferenced_applicable_architecture",
    "unreferenced_applicable_standards",
]
