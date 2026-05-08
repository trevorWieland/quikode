"""Plan 35 PR-A: serialization helpers for `EvaluationContract`.

Lives outside `evaluation_contract.py` to keep that module under the
600-line architecture budget. The contract gained a five-stage shape
plus loaded-corpus persistence in plan 35; the resulting boilerplate
for to-jsonable / from-jsonable would push the parent file over budget.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .architecture_docs import ArchitectureCorpus, ArchitectureDoc
from .standards_profiles import StandardsDoc, StandardsProfile

if TYPE_CHECKING:
    from .evaluation_contract import EvaluationContract


_VALID_STAGE_NAMES: tuple[str, ...] = (
    "local_ci",
    "rubric",
    "standards",
    "architecture",
    "behavior",
)


def _coerce_stage_name(value: object) -> str:
    """Validate that `value` is a known stage name; return it as a str.
    The caller (`_narrow_stage_name` in `evaluation_contract.py`) does
    the final Literal narrowing so this module stays free of cyclic
    imports against `evaluation_contract.StageName`.
    """
    text = str(value)
    if text not in _VALID_STAGE_NAMES:
        raise ValueError(f"unknown stage name {text!r}; expected one of {list(_VALID_STAGE_NAMES)!r}")
    return text


def _str_tuple_from_list(blob: object) -> tuple[str, ...]:
    if not isinstance(blob, list):
        return ()
    return tuple(str(x) for x in blob)


def _importance_from_jsonable(value: object) -> Literal["low", "medium", "high", "critical"]:
    text = str(value)
    if text == "low":
        return "low"
    if text == "medium":
        return "medium"
    if text == "high":
        return "high"
    if text == "critical":
        return "critical"
    raise ValueError(f"importance {text!r} not in low/medium/high/critical")


# ----- to-jsonable -----


def _standards_doc_to_jsonable(doc: StandardsDoc) -> dict[str, object]:
    return {
        "profile": doc.profile,
        "category": doc.category,
        "name": doc.name,
        "path": str(doc.path),
        "repo_relative": doc.repo_relative,
        "importance": doc.importance,
        "applies_to": list(doc.applies_to),
        "applies_to_languages": list(doc.applies_to_languages),
        "applies_to_domains": list(doc.applies_to_domains),
        "body": doc.body,
        "sections": list(doc.sections),
    }


def _profile_to_jsonable(profile: StandardsProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "root": str(profile.root),
        "docs": [_standards_doc_to_jsonable(d) for d in profile.docs],
    }


def _arch_doc_to_jsonable(doc: ArchitectureDoc) -> dict[str, object]:
    return {
        "path": str(doc.path),
        "repo_relative": doc.repo_relative,
        "title": doc.title,
        "sections": list(doc.sections),
        "body": doc.body,
    }


def _arch_corpus_to_jsonable(corpus: ArchitectureCorpus) -> dict[str, object]:
    return {
        "root": str(corpus.root),
        "docs": [_arch_doc_to_jsonable(d) for d in corpus.docs],
    }


def to_jsonable(contract: EvaluationContract) -> dict[str, object]:
    """Stable dict shape: explicit field ordering for deterministic JSON."""
    return {
        "task_id": contract.task_id,
        "local_ci": asdict(contract.local_ci),
        "rubric": asdict(contract.rubric),
        "standards": {
            "name": contract.standards.name,
            "one_line": contract.standards.one_line,
            "threshold": contract.standards.threshold,
            "grading_template": contract.standards.grading_template,
            "profiles": [_profile_to_jsonable(p) for p in contract.standards.profiles],
            "source_text": contract.standards.source_text,
        },
        "architecture": {
            "name": contract.architecture.name,
            "one_line": contract.architecture.one_line,
            "threshold": contract.architecture.threshold,
            "grading_template": contract.architecture.grading_template,
            "corpus": _arch_corpus_to_jsonable(contract.architecture.corpus),
            "source_text": contract.architecture.source_text,
        },
        "behavior": asdict(contract.behavior),
    }


# ----- from-jsonable -----


def _standards_doc_from_jsonable(blob: dict[str, object]) -> StandardsDoc:
    return StandardsDoc(
        profile=str(blob.get("profile", "")),
        category=str(blob.get("category", "")),
        name=str(blob.get("name", "")),
        path=Path(str(blob.get("path", ""))),
        repo_relative=str(blob.get("repo_relative", "")),
        importance=_importance_from_jsonable(blob.get("importance", "low")),
        applies_to=_str_tuple_from_list(blob.get("applies_to")),
        applies_to_languages=_str_tuple_from_list(blob.get("applies_to_languages")),
        applies_to_domains=_str_tuple_from_list(blob.get("applies_to_domains")),
        body=str(blob.get("body", "")),
        sections=_str_tuple_from_list(blob.get("sections")),
    )


def _profile_from_jsonable(blob: dict[str, object]) -> StandardsProfile:
    docs_blob = blob.get("docs", [])
    docs: list[StandardsDoc] = []
    if isinstance(docs_blob, list):
        for d in docs_blob:
            if isinstance(d, dict):
                docs.append(_standards_doc_from_jsonable({str(k): v for k, v in d.items()}))
    return StandardsProfile(
        name=str(blob.get("name", "")),
        root=Path(str(blob.get("root", ""))),
        docs=tuple(docs),
    )


def _arch_doc_from_jsonable(blob: dict[str, object]) -> ArchitectureDoc:
    return ArchitectureDoc(
        path=Path(str(blob.get("path", ""))),
        repo_relative=str(blob.get("repo_relative", "")),
        title=str(blob.get("title", "")),
        sections=_str_tuple_from_list(blob.get("sections")),
        body=str(blob.get("body", "")),
    )


def _arch_corpus_from_jsonable(blob: dict[str, object]) -> ArchitectureCorpus:
    docs_blob = blob.get("docs", [])
    docs: list[ArchitectureDoc] = []
    if isinstance(docs_blob, list):
        for d in docs_blob:
            if isinstance(d, dict):
                docs.append(_arch_doc_from_jsonable({str(k): v for k, v in d.items()}))
    return ArchitectureCorpus(
        root=Path(str(blob.get("root", ""))),
        docs=tuple(docs),
    )


def _get_str(blob: dict[str, object], key: str, default: str = "") -> str:
    v = blob.get(key)
    if v is None:
        return default
    return str(v)


def stage_kwargs(blob: object) -> dict[str, object]:
    """Decode a generic StageRubric blob into kwargs. Caller passes them
    to `StageRubric(**stage_kwargs(blob))` to construct the dataclass."""
    if not isinstance(blob, dict):
        raise ValueError(f"expected stage dict, got {type(blob).__name__}")
    typed: dict[str, object] = {str(k): v for k, v in blob.items()}
    return {
        "name": _coerce_stage_name(typed.get("name")),
        "one_line": _get_str(typed, "one_line"),
        "threshold": _get_str(typed, "threshold"),
        "grading_template": _get_str(typed, "grading_template"),
        "source_text": _get_str(typed, "source_text"),
    }


def standards_stage_kwargs(blob: object) -> dict[str, object]:
    """Decode the standards stage blob into kwargs for StandardsStageRubric."""
    if not isinstance(blob, dict):
        raise ValueError(f"expected standards stage dict, got {type(blob).__name__}")
    typed: dict[str, object] = {str(k): v for k, v in blob.items()}
    profiles_blob = typed.get("profiles", [])
    profiles: list[StandardsProfile] = []
    if isinstance(profiles_blob, list):
        for p in profiles_blob:
            if isinstance(p, dict):
                profiles.append(_profile_from_jsonable({str(k): v for k, v in p.items()}))
    return {
        "one_line": _get_str(typed, "one_line"),
        "threshold": _get_str(typed, "threshold"),
        "grading_template": _get_str(typed, "grading_template"),
        "profiles": tuple(profiles),
        "source_text": _get_str(typed, "source_text"),
    }


def architecture_stage_kwargs(blob: object) -> dict[str, object]:
    """Decode the architecture stage blob into kwargs for ArchitectureStageRubric."""
    if not isinstance(blob, dict):
        raise ValueError(f"expected architecture stage dict, got {type(blob).__name__}")
    typed: dict[str, object] = {str(k): v for k, v in blob.items()}
    corpus_blob = typed.get("corpus", {})
    corpus = (
        _arch_corpus_from_jsonable({str(k): v for k, v in corpus_blob.items()})
        if isinstance(corpus_blob, dict)
        else ArchitectureCorpus(root=Path(), docs=())
    )
    return {
        "one_line": _get_str(typed, "one_line"),
        "threshold": _get_str(typed, "threshold"),
        "grading_template": _get_str(typed, "grading_template"),
        "corpus": corpus,
        "source_text": _get_str(typed, "source_text"),
    }


__all__ = [
    "architecture_stage_kwargs",
    "stage_kwargs",
    "standards_stage_kwargs",
    "to_jsonable",
]
