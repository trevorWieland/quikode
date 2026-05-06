from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
from quikode.workspace import seed_from_main


def _cfg(tmp_path: Path, dag_path: Path) -> Config:
    repo = tmp_path / "repo"
    repo.mkdir()
    return Config(
        repo_path=repo,
        dag_path=dag_path,
        state_dir=tmp_path / ".quikode",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        log_dir=tmp_path / ".quikode" / "logs",
    )


def _fixture_dag() -> Path:
    return Path(__file__).parent / "fixtures" / "tanren_dag.json"


def _store(cfg: Config) -> Store:
    return Store(cfg.state_dir / "quikode.db")


def test_seed_from_main_marks_metadata_merged_nodes(tmp_path: Path):
    cfg = _cfg(tmp_path, _fixture_dag())
    store = _store(cfg)

    result = seed_from_main(cfg, store)

    assert result.merged == {
        "F-0001": "dag:merged_in_main=true",
        "R-0001": 'dag:status="merged"',
    }
    f = store.get("F-0001")
    r = store.get("R-0001")
    assert f is not None
    assert r is not None
    assert f["state"] == State.MERGED.value
    assert r["state"] == State.MERGED.value
    assert store.get("R-0002") is None


def test_seed_from_main_accepts_explicit_evidence_file(tmp_path: Path):
    cfg = _cfg(tmp_path, _fixture_dag())
    evidence = tmp_path / "merged.json"
    evidence.write_text(json.dumps({"R-0002": "manual PR evidence"}))
    store = _store(cfg)

    result = seed_from_main(cfg, store, merged_nodes_file=evidence)

    assert result.merged["R-0002"] == "explicit-file:manual PR evidence"
    row = store.get("R-0002")
    assert row is not None
    assert row["seed_source"] == "explicit-file"
    assert row["state"] == State.MERGED.value


def test_seed_from_main_accepts_exact_git_subject_evidence(tmp_path: Path):
    cfg = _cfg(tmp_path, _fixture_dag())
    subprocess.run(["git", "init"], cwd=cfg.repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cfg.repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=cfg.repo_path, check=True)
    (cfg.repo_path / "file.txt").write_text("x")
    subprocess.run(["git", "add", "file.txt"], cwd=cfg.repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "R-0002: land behavior"], cwd=cfg.repo_path, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=cfg.repo_path, check=True)
    store = _store(cfg)

    result = seed_from_main(cfg, store)

    assert result.merged["R-0002"] == "git-subject:R-0002: land behavior"
    row = store.get("R-0002")
    assert row is not None
    assert row["state"] == State.MERGED.value


def test_unseeded_nodes_obey_dependency_order(tmp_path: Path):
    cfg = _cfg(tmp_path, _fixture_dag())
    store = _store(cfg)
    seed_from_main(cfg, store)
    dag = DAG.load(cfg.dag_path)

    ready = dag.ready_nodes(store.completed_ids(), store.active_ids())

    assert [node.id for node in ready] == ["R-0002", "R-0003"]


def test_unknown_state_at_startup_fails(tmp_path: Path):
    cfg = _cfg(tmp_path, _fixture_dag())
    store = _store(cfg)
    store.upsert_pending("BROKEN")
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET state = 'not-real' WHERE id = 'BROKEN'")
    store.conn.close()

    with pytest.raises(ValueError, match="invalid task state"):
        _store(cfg)
