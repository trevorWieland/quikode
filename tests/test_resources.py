"""Resource math: max_parallel computation from host headroom."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from quikode.cli import _compute_max_parallel
from quikode.config import Config
from quikode.docker_env import _parse_mem_string, workspace_label


def _cfg(**kw):
    return Config(repo_path=Path("/tmp"), dag_path=Path("/tmp"), **kw)


def test_cpu_bounded():
    cfg = _cfg(cpu_per_task=4, mem_per_task_gb=8, host_reserved_cpu=4, host_reserved_mem_gb=16)
    cap, _ = _compute_max_parallel(cfg, {"cpus": 24, "mem_bytes": 80 * 1024**3})
    # avail cpu = 24-4 = 20 ; cpu by task = 20//4 = 5
    # avail mem = 80-16 = 64 ; mem by task = 64//8 = 8
    # min = 5
    assert cap == 5


def test_mem_bounded():
    cfg = _cfg(cpu_per_task=2, mem_per_task_gb=16, host_reserved_cpu=2, host_reserved_mem_gb=8)
    cap, _ = _compute_max_parallel(cfg, {"cpus": 24, "mem_bytes": 80 * 1024**3})
    # avail cpu = 24-2 = 22 ; cpu by task = 22//2 = 11
    # avail mem = 80-8 = 72 ; mem by task = 72//16 = 4
    # min = 4
    assert cap == 4


def test_floor_at_one():
    cfg = _cfg(cpu_per_task=8, mem_per_task_gb=32, host_reserved_cpu=4, host_reserved_mem_gb=8)
    cap, _ = _compute_max_parallel(cfg, {"cpus": 4, "mem_bytes": 16 * 1024**3})
    # 4-4=0 cpus avail; 16-8=8 mem avail; both budgets are 0/8 = 0; min=0; cap clamped to 1
    assert cap == 1


def test_zero_per_task_rejected_by_validation():
    """Pydantic config rejects per-task = 0 since it has no useful meaning;
    the prior 'unlimited' semantics have been dropped in favor of strict bounds.
    """
    with pytest.raises(ValidationError):
        _cfg(cpu_per_task=0)


def test_explanation_string_includes_numbers():
    cfg = _cfg(cpu_per_task=4, mem_per_task_gb=12, host_reserved_cpu=4, host_reserved_mem_gb=16)
    cap, expl = _compute_max_parallel(cfg, {"cpus": 24, "mem_bytes": 80 * 1024**3})
    assert "24 cpus" in expl
    assert "80 GB" in expl
    assert f"⇒ {cap}" in expl


# ---------- mem string parsing ----------


def test_parse_mem_string_GiB():
    assert _parse_mem_string("1.5GiB") == int(1.5 * 1024**3)


def test_parse_mem_string_MiB():
    assert _parse_mem_string("512MiB") == 512 * 1024**2


def test_parse_mem_string_invalid():
    assert _parse_mem_string("bogus") == 0
    assert _parse_mem_string("") == 0


# ---------- workspace label scoping ----------


def test_workspace_label_stable_for_same_path(tmp_path):
    cfg = _cfg(state_dir=tmp_path / ".quikode")
    a = workspace_label(cfg)
    b = workspace_label(cfg)
    assert a == b
    assert a.startswith("qk_workspace=")
    assert len(a.split("=")[1]) == 8  # 8-hex token


def test_workspace_label_differs_per_path(tmp_path):
    a = workspace_label(_cfg(state_dir=tmp_path / "a"))
    b = workspace_label(_cfg(state_dir=tmp_path / "b"))
    assert a != b
