"""Launch-time validation for runtime-critical workspace configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .architecture_docs import load_architecture
from .config import Config
from .standards_profiles import load_profiles


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    message: str


class ConfigValidationError(RuntimeError):
    """Raised when the daemon/run path cannot safely start workers."""

    def __init__(self, issues: list[ConfigIssue]):
        self.issues = tuple(issues)
        details = "\n".join(f"- {issue.field}: {issue.message}" for issue in issues)
        super().__init__(f"invalid quikode launch configuration:\n{details}")


def validate_launch_config(cfg: Config) -> None:
    """Validate config required for autonomous worker execution.

    This intentionally runs before daemon detach / worker scheduling. Audit
    doc configuration is part of the launch contract because missing corpora
    otherwise become runtime audit failures after tasks have already spent
    doer/checker cycles.
    """
    issues: list[ConfigIssue] = []
    _check_path(issues, "repo_path", cfg.repo_path, kind="dir")
    _check_path(issues, "dag_path", cfg.dag_path, kind="file")
    if not (cfg.local_ci_command or "").strip():
        issues.append(ConfigIssue("local_ci_command", "must be non-empty for the pre-PR gate"))
    _check_standards(issues, cfg)
    _check_architecture(issues, cfg)
    if issues:
        raise ConfigValidationError(issues)


def _check_path(issues: list[ConfigIssue], field: str, path: Path, *, kind: str) -> None:
    if kind == "dir" and not path.is_dir():
        issues.append(ConfigIssue(field, f"directory does not exist: {path}"))
    elif kind == "file" and not path.is_file():
        issues.append(ConfigIssue(field, f"file does not exist: {path}"))


def _check_standards(issues: list[ConfigIssue], cfg: Config) -> None:
    if not cfg.standards_profiles:
        issues.append(
            ConfigIssue(
                "standards_profiles",
                "must list at least one profile; runtime standards audits cannot run with an empty catalog",
            )
        )
        return
    try:
        profiles = load_profiles(cfg)
    except Exception as exc:
        issues.append(ConfigIssue("standards_profiles_dir", str(exc)))
        return
    missing_docs = [profile.name for profile in profiles if not profile.docs]
    if missing_docs:
        issues.append(
            ConfigIssue(
                "standards_profiles",
                f"profile(s) contain no markdown docs: {', '.join(missing_docs)}",
            )
        )
    if not any(profile.docs for profile in profiles):
        issues.append(
            ConfigIssue(
                "standards_profiles_dir",
                f"no standards profile docs loaded from {cfg.standards_profiles_dir}",
            )
        )


def _check_architecture(issues: list[ConfigIssue], cfg: Config) -> None:
    corpus = load_architecture(cfg)
    if not corpus.docs:
        issues.append(
            ConfigIssue(
                "architecture_docs_dir",
                f"no architecture docs loaded from {corpus.root}; set architecture_docs_dir + architecture_doc_globs",
            )
        )


__all__ = ["ConfigIssue", "ConfigValidationError", "validate_launch_config"]
