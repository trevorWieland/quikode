"""v3 Phase A guard: no `git commit --amend` anywhere in the codebase.

Per-subtask commits (Phase A) only stay distinct on the branch if no
subsequent phase amends them. The whole-spec doer (`_do()`, used only
for final-check fixup) just runs the agent — it never invokes git
itself. The agent's prompt (`prompts/doer.md`) tells it explicitly not
to commit. The orchestrator's commit path (`github.commit_all` /
`worktree.commit_subtask`) does plain `git commit -m ...`, never
`--amend`.

This test is the regression guard. If any future change introduces an
`--amend` path, fix the new code or update this test deliberately.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _scan(target_dir: Path, exclusions: tuple[str, ...] = ()) -> list[tuple[Path, int, str]]:
    """Return (path, lineno, line) for every line containing 'amend'."""
    hits: list[tuple[Path, int, str]] = []
    for path in target_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".md"}:
            continue
        if any(ex in str(path) for ex in exclusions):
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if "amend" in line.lower():
                hits.append((path, i, line))
    return hits


def test_no_amend_in_quikode_source():
    """Production source files (quikode/) must not call `git commit --amend`."""
    hits = _scan(ROOT / "quikode")
    # Filter false positives — comments are fine if they explicitly
    # describe the *absence* of amend behavior. We allow any line that
    # mentions "amend" purely descriptively (i.e. says NO amend).
    real_hits = [
        (p, n, line)
        for (p, n, line) in hits
        if "amend" in line.lower()
        and "no `git commit --amend`" not in line
        and "no --amend" not in line
        and "no amend" not in line.lower()
    ]
    # Stronger check: no line should call `--amend` as a flag.
    flag_hits = [(p, n, line) for (p, n, line) in real_hits if "--amend" in line]
    assert flag_hits == [], (
        "Found `--amend` in production code (would silently rewrite per-subtask commits):\n"
        + "\n".join(f"  {p}:{n}: {line.strip()}" for p, n, line in flag_hits)
    )


def test_no_amend_in_prompts():
    """Agent prompts must not instruct the doer/checker/etc. to amend.
    A prompt-injected `--amend` would silently rewrite per-subtask
    commits and undo the v3 Phase A invariant."""
    hits = _scan(ROOT / "prompts")
    flag_hits = [(p, n, line) for (p, n, line) in hits if "--amend" in line]
    assert flag_hits == [], (
        "Found `--amend` instruction in prompt template (would tell agent to silently rewrite history):\n"
        + "\n".join(f"  {p}:{n}: {line.strip()}" for p, n, line in flag_hits)
    )
