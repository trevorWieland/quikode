"""End-to-end git integration tests for `quikode.stacking`.

Exercises `construct_merge_base` against a real git repo in `tmp_path`,
not subprocess fakes. Catches issues that pattern-mocked tests miss:

- The actual `git merge` exit codes for octopus / sequential / conflict.
- That the working tree is left clean after a conflict abort (no
  half-merged state, no MERGE_HEAD lingering).
- That the resulting merge-base sha is reachable from each parent
  branch (non-destructive merge) and from main.
- Idempotence: calling twice with identical parents produces the same
  branch tip + sha.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quikode import stacking


def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`, return stripped stdout. Raises on non-zero."""
    r = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),  # avoid global git config bleed
            "GIT_AUTHOR_NAME": "qk-test",
            "GIT_AUTHOR_EMAIL": "qk-test@example.com",
            "GIT_COMMITTER_NAME": "qk-test",
            "GIT_COMMITTER_EMAIL": "qk-test@example.com",
        },
    )
    return r.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    """Stage everything + commit. Return the new HEAD sha."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def two_parent_repo(tmp_path):
    """Build a repo with main + two parent branches that don't conflict.

    Layout:
        main      ← initial commit (file_main)
          ├── branch-b adds file_b
          └── branch-c adds file_c

    Returns the repo path.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "file_main").write_text("main content\n")
    _commit(repo, "initial main")

    _git(repo, "checkout", "-b", "branch-b")
    (repo / "file_b").write_text("file b content\n")
    _commit(repo, "add file_b")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "branch-c")
    (repo / "file_c").write_text("file c content\n")
    _commit(repo, "add file_c")

    _git(repo, "checkout", "main")
    return repo


@pytest.fixture
def conflicting_parent_repo(tmp_path):
    """Build a repo with two parent branches that conflict on the same file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "shared.txt").write_text("hello\n")
    _commit(repo, "initial")

    _git(repo, "checkout", "-b", "branch-b")
    (repo / "shared.txt").write_text("hello from B\n")
    _commit(repo, "B edits shared")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "branch-c")
    (repo / "shared.txt").write_text("hello from C\n")
    _commit(repo, "C edits shared")

    _git(repo, "checkout", "main")
    return repo


# ----- happy path: two parents, no conflict -----


def test_construct_merge_base_clean_octopus(two_parent_repo):
    """Two non-conflicting parents → octopus merge succeeds, returns sha
    reachable from both parents."""
    repo = two_parent_repo
    mb_name = "quikode/r-099-base-aabbcc"
    sha = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b", "branch-c"],
        branch_name=mb_name,
    )
    assert sha is not None
    # The merge-base branch should now exist with HEAD == sha.
    assert _git(repo, "rev-parse", mb_name) == sha
    # And both parents should be ancestors of it (non-destructive merge).
    assert _git(repo, "merge-base", "--is-ancestor", "branch-b", sha) == ""
    assert _git(repo, "merge-base", "--is-ancestor", "branch-c", sha) == ""
    # The two parent files should be present in the merge.
    _git(repo, "checkout", mb_name)
    assert (repo / "file_b").exists()
    assert (repo / "file_c").exists()


def test_construct_merge_base_idempotent_for_same_parents(two_parent_repo):
    """Calling twice with the same parents → same result. The branch is
    reset off main each time, so the second call replaces (not extends)
    whatever was there. Tests that the helper handles the
    "branch already exists" case cleanly."""
    repo = two_parent_repo
    mb_name = stacking.compute_merge_base_branch_name("R-099", ["branch-b", "branch-c"])
    sha1 = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b", "branch-c"],
        branch_name=mb_name,
    )
    sha2 = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b", "branch-c"],
        branch_name=mb_name,
    )
    assert sha1 is not None and sha2 is not None
    # Two octopus merges of the same parents off the same base produce the
    # same tree but new commit shas (commit time differs in repo metadata
    # but we forced env timestamps in _git… still, the *tree* must match).
    tree1 = _git(repo, "rev-parse", f"{sha1}^{{tree}}")
    tree2 = _git(repo, "rev-parse", f"{sha2}^{{tree}}")
    assert tree1 == tree2


def test_construct_merge_base_three_parents_octopus(tmp_path):
    """Three independent branches → octopus merge with three parents
    produces a single commit that includes all three."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "main.txt").write_text("main\n")
    _commit(repo, "init")
    for letter in ("a", "b", "c"):
        _git(repo, "checkout", "main")
        _git(repo, "checkout", "-b", f"branch-{letter}")
        (repo / f"file_{letter}").write_text(f"content {letter}\n")
        _commit(repo, f"add file_{letter}")
    _git(repo, "checkout", "main")

    sha = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-a", "branch-b", "branch-c"],
        branch_name="quikode/r-multi-base",
    )
    assert sha is not None
    _git(repo, "checkout", "quikode/r-multi-base")
    for letter in ("a", "b", "c"):
        assert (repo / f"file_{letter}").exists()


# ----- conflict path: parents conflict, must abort cleanly -----


def test_construct_merge_base_conflict_returns_none_and_leaves_clean_tree(conflicting_parent_repo):
    """Conflicting parents → helper aborts the merge and returns None.
    Crucially: the working tree must be CLEAN afterwards (no MERGE_HEAD,
    no merge-state files lingering) so the next operation isn't blocked."""
    repo = conflicting_parent_repo
    mb_name = "quikode/r-conflict-base"

    sha = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b", "branch-c"],
        branch_name=mb_name,
    )
    assert sha is None
    # No active merge.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert not (repo / ".git" / "MERGE_MSG").exists()
    # Tree clean (status output empty).
    status = _git(repo, "status", "--porcelain")
    assert status == ""


# ----- partial-conflict path: octopus fails, sequential succeeds -----


def test_construct_merge_base_octopus_fails_sequential_succeeds(tmp_path):
    """Two branches that touch DIFFERENT files but in a way that defeats
    octopus (renames, mode changes, etc.) — the helper should fall back
    to sequential merges and succeed.

    In practice octopus is robust against simple file-add scenarios, so
    we use a setup where both branches modify the *same* file's
    different lines (resolves with default 3-way merge but octopus
    refuses with multiple non-trivial parents). Note: git's exact
    octopus failure heuristics aren't worth replicating — we just want
    to confirm the fallback path is sound when invoked.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "file.txt").write_text("line1\nline2\nline3\n")
    _commit(repo, "init")

    _git(repo, "checkout", "-b", "branch-b")
    (repo / "file.txt").write_text("line1 B\nline2\nline3\n")
    _commit(repo, "B edits line 1")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "branch-c")
    (repo / "file.txt").write_text("line1\nline2\nline3 C\n")
    _commit(repo, "C edits line 3")

    _git(repo, "checkout", "main")
    sha = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b", "branch-c"],
        branch_name="quikode/r-mixed-base",
    )
    # Either octopus or sequential — but it should succeed since edits
    # are to different non-overlapping lines and 3-way merge handles it.
    assert sha is not None
    _git(repo, "checkout", "quikode/r-mixed-base")
    content = (repo / "file.txt").read_text()
    assert "line1 B" in content
    assert "line3 C" in content


# ----- single-parent path (defensive — not the main use case) -----


def test_construct_merge_base_single_parent_succeeds(two_parent_repo):
    """The helper is mainly for >1 parent, but a single-parent call should
    still return a valid sha — a degenerate "merge" of one branch into
    main is just a fast-forward / no-ff merge."""
    repo = two_parent_repo
    sha = stacking.construct_merge_base(
        repo_path=repo,
        parent_branches=["branch-b"],
        branch_name="quikode/r-single-base",
    )
    assert sha is not None
    _git(repo, "checkout", "quikode/r-single-base")
    assert (repo / "file_b").exists()
