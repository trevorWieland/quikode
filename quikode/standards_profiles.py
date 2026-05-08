"""Plan 35 PR-A: standards-profile loader.

Loads standards profile docs from `cfg.standards_profiles_dir / <profile>`
trees. Each `*.md` file ships YAML-style frontmatter declaring
`kind: standard`, `name`, `category`, `importance`, `applies_to`,
`applies_to_languages`, `applies_to_domains`. Parsed into frozen
dataclasses for downstream prompt rendering and validator dispatch.

The frontmatter parser is intentionally hand-rolled — quikode does not
add a PyYAML dep for this. Format:

  ---
  key: value
  list_key:
    - item1
    - item2
  ---

Malformed frontmatter or a missing required key raises `RuntimeError`
naming the offending file path (no silent skipping).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .config import Config


Importance = Literal["low", "medium", "high", "critical"]
_VALID_IMPORTANCE: tuple[Importance, ...] = ("low", "medium", "high", "critical")
_REQUIRED_KEYS: tuple[str, ...] = (
    "kind",
    "name",
    "category",
    "importance",
)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class StandardsDoc:
    """One standards-profile doc (a single .md file under a profile tree)."""

    profile: str
    category: str
    name: str
    path: Path
    repo_relative: str
    importance: Importance
    applies_to: tuple[str, ...]
    applies_to_languages: tuple[str, ...]
    applies_to_domains: tuple[str, ...]
    body: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class StandardsProfile:
    """One profile (e.g. `rust-cargo`) — a directory of standards docs."""

    name: str
    root: Path
    docs: tuple[StandardsDoc, ...]


# ----- frontmatter parser -----


def _strip_quotes(val: str) -> str:
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def _parse_inline_list(val: str) -> list[str]:
    inner = val[1:-1].strip()
    if not inner:
        return []
    return [_strip_quotes(piece.strip()) for piece in inner.split(",")]


def _split_frontmatter_block(text: str, path: Path) -> tuple[list[str], str] | None:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        raise RuntimeError(f"frontmatter at {path}: opening `---` has no matching closing `---`")
    return lines[1:end_idx], "\n".join(lines[end_idx + 1 :])


def _flush_list(fm: dict[str, object], key: str | None, items: list[str]) -> tuple[None, list[str]]:
    if key is not None:
        fm[key] = items
    return None, []


def _consume_bullet_line(
    raw_line: str, current_list_key: str | None, current_list: list[str], path: Path
) -> None:
    stripped = raw_line.lstrip()
    if current_list_key is None:
        raise RuntimeError(
            f"frontmatter at {path}: bullet line {raw_line!r} appears outside a list-typed key"
        )
    current_list.append(_strip_quotes(stripped[2:].strip()))


def _consume_kv_line(raw_line: str, fm: dict[str, object], path: Path) -> tuple[str | None, list[str]]:
    """Parse one `key: value` (or `key:` list-open) frontmatter line.

    Returns the (current_list_key, current_list) state — non-None
    `current_list_key` means the next bullet lines accrue into a list.
    """
    if ":" not in raw_line:
        raise RuntimeError(
            f"frontmatter at {path}: line {raw_line!r} is not `key: value` and not a `- item` bullet"
        )
    key, _, val = raw_line.partition(":")
    key = key.strip()
    val = val.strip()
    if not key:
        raise RuntimeError(f"frontmatter at {path}: empty key in line {raw_line!r}")
    if val == "":
        return key, []
    if val.startswith("[") and val.endswith("]"):
        fm[key] = _parse_inline_list(val)
        return None, []
    fm[key] = _strip_quotes(val)
    return None, []


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    """Split a doc into (frontmatter_dict, body_after_frontmatter).

    Returns ({}, text) when no frontmatter delimiter is present. Raises
    `RuntimeError` (with `path`) when the opening `---` is found but the
    closing `---` is missing, or when a key:value line is malformed.
    """
    split = _split_frontmatter_block(text, path)
    if split is None:
        return {}, text
    fm_lines, body = split
    fm: dict[str, object] = {}
    current_list_key: str | None = None
    current_list: list[str] = []
    for raw_line in fm_lines:
        if not raw_line.strip():
            current_list_key, current_list = _flush_list(fm, current_list_key, current_list)
            continue
        if raw_line.lstrip().startswith("- "):
            _consume_bullet_line(raw_line, current_list_key, current_list, path)
            continue
        # Close any in-progress list before starting a new key.
        current_list_key, current_list = _flush_list(fm, current_list_key, current_list)
        current_list_key, current_list = _consume_kv_line(raw_line, fm, path)
    _flush_list(fm, current_list_key, current_list)
    return fm, body


def _to_str(val: object, path: Path, key: str) -> str:
    if not isinstance(val, str):
        raise RuntimeError(f"frontmatter at {path}: key {key!r} must be a string, got {type(val).__name__}")
    return val


def _to_str_tuple(val: object, path: Path, key: str) -> tuple[str, ...]:
    if val is None:
        return ()
    if isinstance(val, str):
        # Permit a single-string scalar where a list was expected — coerces.
        return (val,)
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            if not isinstance(item, str):
                raise RuntimeError(
                    f"frontmatter at {path}: list key {key!r} contains non-string item {item!r}"
                )
            out.append(item)
        return tuple(out)
    raise RuntimeError(
        f"frontmatter at {path}: key {key!r} must be a list or string, got {type(val).__name__}"
    )


def _parse_sections(body: str) -> tuple[str, ...]:
    """Extract `#`/`##`/`###` headings (text portion only)."""
    return tuple(m.group(2).strip() for m in _HEADING_RE.finditer(body))


def _build_doc(
    *,
    profile: str,
    profile_root: Path,
    repo_root: Path,
    path: Path,
) -> StandardsDoc:
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text, path)
    for key in _REQUIRED_KEYS:
        if key not in fm:
            raise RuntimeError(f"frontmatter at {path}: missing required key {key!r}")
    importance_raw = _to_str(fm["importance"], path, "importance")
    if importance_raw not in _VALID_IMPORTANCE:
        raise RuntimeError(
            f"frontmatter at {path}: importance must be one of "
            f"{list(_VALID_IMPORTANCE)!r}, got {importance_raw!r}"
        )
    # Locked Literal narrowing without a type-ignore: re-pick from the
    # known tuple to satisfy ty's exhaustive check.
    importance: Importance = "low"
    for known in _VALID_IMPORTANCE:
        if known == importance_raw:
            importance = known
            break
    try:
        repo_relative = str(path.relative_to(repo_root))
    except ValueError:
        repo_relative = str(path)
    return StandardsDoc(
        profile=profile,
        category=_to_str(fm["category"], path, "category"),
        name=_to_str(fm["name"], path, "name"),
        path=path,
        repo_relative=repo_relative,
        importance=importance,
        applies_to=_to_str_tuple(fm.get("applies_to"), path, "applies_to"),
        applies_to_languages=_to_str_tuple(fm.get("applies_to_languages"), path, "applies_to_languages"),
        applies_to_domains=_to_str_tuple(fm.get("applies_to_domains"), path, "applies_to_domains"),
        body=body,
        sections=_parse_sections(body),
    )


# ----- public API -----


def load_profiles(cfg: Config) -> tuple[StandardsProfile, ...]:
    """Walk `cfg.standards_profiles_dir / <profile>` for each name in
    `cfg.standards_profiles`. Returns an empty tuple when no profiles are
    configured (validators surface this as fail-closed). Each profile's
    `*.md` files are parsed; missing required frontmatter raises with the
    file path.
    """
    repo_root = Path(cfg.repo_path)
    profiles_root = cfg.standards_profiles_dir
    profiles_root_abs = profiles_root if profiles_root.is_absolute() else repo_root / profiles_root
    profiles_root_abs = profiles_root_abs.resolve()
    out: list[StandardsProfile] = []
    for profile_name in cfg.standards_profiles:
        profile_dir = profiles_root_abs / profile_name
        if not profile_dir.exists():
            raise RuntimeError(
                f"standards profile {profile_name!r} listed in "
                f"cfg.standards_profiles but directory {profile_dir} "
                "does not exist"
            )
        if not profile_dir.is_dir():
            raise RuntimeError(f"standards profile path {profile_dir} is not a directory")
        docs: list[StandardsDoc] = []
        for md_path in sorted(profile_dir.rglob("*.md")):
            if not md_path.is_file():
                continue
            docs.append(
                _build_doc(
                    profile=profile_name,
                    profile_root=profile_dir,
                    repo_root=repo_root,
                    path=md_path,
                )
            )
        out.append(
            StandardsProfile(
                name=profile_name,
                root=profile_dir,
                docs=tuple(docs),
            )
        )
    return tuple(out)


def find_doc(profiles: tuple[StandardsProfile, ...], doc_path: str) -> StandardsDoc | None:
    """Resolve a planner-cited `doc_path` (repo-relative) to a loaded
    `StandardsDoc`. Returns None when the path doesn't match any profile
    doc — the validator turns that into the bucket-correction error.
    """
    target = doc_path.strip()
    for profile in profiles:
        for doc in profile.docs:
            if doc.repo_relative == target:
                return doc
    return None


def find_section(doc: StandardsDoc, section: str) -> bool:
    """Case-insensitive, whitespace-folded heading match."""
    needle = " ".join(section.split()).lower()
    if not needle:
        return False
    return any(" ".join(h.split()).lower() == needle for h in doc.sections)


def render_profile_catalog(profiles: tuple[StandardsProfile, ...], char_cap: int = 30_000) -> str:
    """Render a profile catalog (per-profile header → bullet list of docs
    with their applies_to_languages + section names). Used as the
    standards-stage `source_text` per Plan 35 §3.5.
    """
    if not profiles:
        return (
            "(no standards profiles configured — set "
            "`standards_profiles_dir` + `standards_profiles` in quikode "
            "config; the standards audit will report a config_error)"
        )
    lines: list[str] = []
    for profile in profiles:
        lines.append(f"## profile: {profile.name}")
        lines.append("")
        if not profile.docs:
            lines.append(f"(empty profile — no .md files under {profile.root})")
            lines.append("")
            continue
        for doc in profile.docs:
            langs = ", ".join(doc.applies_to_languages) or "(any)"
            sections = ", ".join(doc.sections) or "(no sections)"
            lines.append(f"- `{doc.repo_relative}` — applies_to_languages: {langs}; sections: {sections}")
        lines.append("")
    text = "\n".join(lines).strip("\n")
    if len(text) > char_cap:
        return text[:char_cap].rstrip() + "\n[STANDARDS PROFILE CATALOG TRUNCATED]"
    return text


__all__ = [
    "Importance",
    "StandardsDoc",
    "StandardsProfile",
    "find_doc",
    "find_section",
    "load_profiles",
    "render_profile_catalog",
]
