"""Plan 35 PR-A: project-architecture document loader.

Architecture docs describe THIS project's subsystem boundaries and
contracts (e.g. tanren's `docs/architecture/subsystems/*.md`). Loaded
into a frozen `ArchitectureCorpus` for prompt rendering and
`validate_architecture_refs` dispatch.

Distinct from `standards_profiles.py`: architecture docs are free-form
(no required frontmatter, no `applies_to` metadata). Optional
frontmatter is tolerated (architecture docs *can* declare frontmatter
for richer metadata). The two modules are deliberately separate so the
two corpora are unconfusable in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .standards_profiles import _parse_frontmatter

if TYPE_CHECKING:
    from .config import Config


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_FIRST_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_NON_HEADING_PARA_RE = re.compile(r"^(?!#)([^\n].*?)$", re.MULTILINE)


@dataclass(frozen=True)
class ArchitectureDoc:
    """One project-architecture document (free-form markdown)."""

    path: Path
    repo_relative: str
    title: str
    sections: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class ArchitectureCorpus:
    """The full set of architecture docs for one workspace."""

    root: Path
    docs: tuple[ArchitectureDoc, ...]


def _parse_sections(body: str) -> tuple[str, ...]:
    return tuple(m.group(2).strip() for m in _HEADING_RE.finditer(body))


def _parse_title(body: str, fallback: str) -> str:
    m = _FIRST_H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return fallback


def _build_doc(*, repo_root: Path, path: Path) -> ArchitectureDoc:
    text = path.read_text(encoding="utf-8")
    # Optional frontmatter — architecture docs MAY declare it but don't
    # have to. Reuse the standards parser; only the body is consumed.
    _fm, body = _parse_frontmatter(text, path)
    if not body:
        body = text
    try:
        repo_relative = str(path.relative_to(repo_root))
    except ValueError:
        repo_relative = str(path)
    return ArchitectureDoc(
        path=path,
        repo_relative=repo_relative,
        title=_parse_title(body, fallback=path.stem),
        sections=_parse_sections(body),
        body=body,
    )


def load_architecture(cfg: Config) -> ArchitectureCorpus:
    """Walk `cfg.architecture_docs_dir` per `cfg.architecture_doc_globs`.

    Returns an empty corpus when the dir doesn't exist or matches no
    files. Validators / audit prompt surface that as fail-closed config
    errors with operator-facing messages.
    """
    repo_root = Path(cfg.repo_path)
    arch_root = cfg.architecture_docs_dir
    arch_root_abs = arch_root if arch_root.is_absolute() else repo_root / arch_root
    arch_root_abs = arch_root_abs.resolve()
    if not arch_root_abs.exists() or not arch_root_abs.is_dir():
        return ArchitectureCorpus(root=arch_root_abs, docs=())
    seen: set[Path] = set()
    docs: list[ArchitectureDoc] = []
    globs = list(cfg.architecture_doc_globs) or ["**/*.md"]
    for pattern in globs:
        for path in sorted(arch_root_abs.glob(pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            docs.append(_build_doc(repo_root=repo_root, path=resolved))
    return ArchitectureCorpus(root=arch_root_abs, docs=tuple(docs))


def find_arch_doc(corpus: ArchitectureCorpus, doc_path: str) -> ArchitectureDoc | None:
    """Resolve a planner-cited repo-relative path against the corpus."""
    target = doc_path.strip()
    for doc in corpus.docs:
        if doc.repo_relative == target:
            return doc
    return None


def find_arch_section(doc: ArchitectureDoc, section: str) -> bool:
    """Case-insensitive, whitespace-folded heading match."""
    needle = " ".join(section.split()).lower()
    if not needle:
        return False
    return any(" ".join(h.split()).lower() == needle for h in doc.sections)


def render_architecture_toc(corpus: ArchitectureCorpus, char_cap: int = 30_000) -> str:
    """Render a per-doc TOC: `repo_relative → title` plus a one-line
    summary parsed from the doc's first non-heading paragraph. Used as
    the architecture-stage `source_text` per Plan 35 §3.5.
    """
    if not corpus.docs:
        return (
            "(no architecture docs found under "
            f"{corpus.root} — set `architecture_docs_dir` + "
            "`architecture_doc_globs` in quikode config)"
        )
    lines: list[str] = []
    for doc in corpus.docs:
        summary = ""
        for m in _NON_HEADING_PARA_RE.finditer(doc.body):
            candidate = m.group(1).strip()
            if candidate:
                summary = candidate
                break
        sections = ", ".join(doc.sections) or "(no sections)"
        lines.append(f"## {doc.repo_relative} — {doc.title}")
        if summary:
            lines.append(f"   summary: {summary[:240]}")
        lines.append(f"   sections: {sections}")
        lines.append("")
    text = "\n".join(lines).strip("\n")
    if len(text) > char_cap:
        return text[:char_cap].rstrip() + "\n[ARCHITECTURE TOC TRUNCATED]"
    return text


__all__ = [
    "ArchitectureCorpus",
    "ArchitectureDoc",
    "find_arch_doc",
    "find_arch_section",
    "load_architecture",
    "render_architecture_toc",
]
