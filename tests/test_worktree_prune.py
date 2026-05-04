"""Stale worktree dir pruning.

When `quikode run` crashes mid-provision, it can leave directories under
`worktree_root/` that git no longer knows about. The next run reuses the
same paths via `git worktree add`, which fails because the path already
exists. Pruning cleans up those orphaned dirs so the next add succeeds.

Two pieces:
- Stale on-disk dirs (not in `git worktree list`): rmtree'd.
- Worktrees git knows about whose paths don't exist: `git worktree prune`
  drops the records.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from quikode.worktree import prune_stale_worktrees


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
    return repo


def test_prune_removes_stale_dir(tmp_path):
    """A directory under worktree_root/ that git doesn't know about gets rm'd."""
    repo = _init_repo(tmp_path)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    # Stale dir — never registered with git.
    stale = wt_root / "r-001-stale"
    stale.mkdir()
    (stale / "leftover.txt").write_text("debris")

    removed = prune_stale_worktrees(repo, wt_root)
    assert stale in removed or stale.resolve() in [p.resolve() for p in removed]
    assert not stale.exists()


def test_prune_preserves_registered_worktree(tmp_path):
    """A live worktree (registered via `git worktree add`) must not be removed."""
    repo = _init_repo(tmp_path)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    live = wt_root / "r-002-live"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat", str(live)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    assert live.exists()

    removed = prune_stale_worktrees(repo, wt_root)
    assert live not in removed
    assert live.exists()


def test_prune_handles_missing_worktree_root(tmp_path):
    """No-op when the worktree root doesn't exist yet."""
    repo = _init_repo(tmp_path)
    removed = prune_stale_worktrees(repo, tmp_path / "does-not-exist")
    assert removed == []


def test_prune_mixed_stale_and_live(tmp_path):
    """Stale + live coexist; only stale is removed."""
    repo = _init_repo(tmp_path)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    live = wt_root / "live-one"
    subprocess.run(
        ["git", "worktree", "add", "-b", "live-branch", str(live)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    stale = wt_root / "stale-one"
    stale.mkdir()
    (stale / "junk.txt").write_text("x")

    removed = prune_stale_worktrees(repo, wt_root)
    assert live.exists()
    assert not stale.exists()
    # Only the stale dir was removed.
    assert any(p.resolve() == stale.resolve() for p in removed)
    assert not any(p.resolve() == live.resolve() for p in removed)


def test_prune_swallows_git_failures(tmp_path):
    """If repo path doesn't exist, prune logs but doesn't raise."""
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    stale = wt_root / "stale-only"
    stale.mkdir()
    # Repo path is bogus — `git worktree list` will fail; we should still
    # rmtree the stale dirs since the registered set is empty.
    bogus_repo = tmp_path / "nonexistent-repo"
    removed = prune_stale_worktrees(bogus_repo, wt_root)
    # In either case, no crash. (Whether stale is removed depends on
    # git's behavior with a bogus cwd; both outcomes are acceptable
    # under "best effort" semantics.)
    assert isinstance(removed, list)
