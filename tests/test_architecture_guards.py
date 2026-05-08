from __future__ import annotations

import ast
from pathlib import Path

from quikode import fsm
from quikode.state import State

ROOT = Path(__file__).resolve().parents[1]


def test_active_docs_do_not_use_removed_runtime_state_names():
    banned = {"awaiting" + "_merge", "responding" + "_to_review"}
    active_docs = [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        ROOT / "orientation.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "runbook-operations.md",
        ROOT / "docs" / "runbook-incident-response.md",
        ROOT / "docs" / "profiles" / "tanren.md",
        ROOT / "docs" / "roadmap.md",
    ]
    allowed_warning = ROOT / "CLAUDE.md"
    for path in active_docs:
        text = path.read_text().lower()
        for term in banned:
            if path == allowed_warning and term in text:
                continue
            assert term not in text, f"{path.relative_to(ROOT)} contains removed state {term!r}"


def test_active_production_code_does_not_use_removed_runtime_state_names():
    banned = {
        "leg" + "acy",
        "awaiting" + "_merge",
        "responding" + "_to_review",
        "whole-spec doer",
        "fallback to " + "leg" + "acy",
    }
    allowed = {
        ROOT / "quikode" / "workspace.py",
    }
    for path in (ROOT / "quikode").rglob("*.py"):
        if "__pycache__" in path.parts or path in allowed:
            continue
        text = path.read_text().lower()
        for term in banned:
            assert term not in text, f"{path.relative_to(ROOT)} contains removed runtime vocabulary {term!r}"


def test_no_skipped_tests_or_inline_suppressions():
    banned = {
        "pytest." + "skip",
        "pytest.mark." + "skip",
        "skip" + "if",
        "x" + "fail",
        "type: " + "ignore",
        "no" + "qa",
        "ruff: " + "no" + "qa",
    }
    for base in (ROOT / "quikode", ROOT / "tests"):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text()
            for term in banned:
                assert term not in text, f"{path.relative_to(ROOT)} contains inline bypass {term!r}"


def test_runtime_code_uses_apply_event_not_direct_transitions():
    allowed = {
        ROOT / "quikode" / "store_tasks.py",
        ROOT / "quikode" / "store_review.py",
        ROOT / "quikode" / "workspace.py",
    }
    for path in (ROOT / "quikode").rglob("*.py"):
        if "__pycache__" in path.parts or path in allowed:
            continue
        text = path.read_text()
        assert ".transition(" not in text, f"{path.relative_to(ROOT)} uses direct Store.transition"


def test_file_lengths_stay_within_architecture_budget():
    production_max = 600
    test_max = 900
    for path in (ROOT / "quikode").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        lines = path.read_text().splitlines()
        assert len(lines) <= production_max, (
            f"{path.relative_to(ROOT)} has {len(lines)} lines; max is {production_max}"
        )
    for path in (ROOT / "tests").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        lines = path.read_text().splitlines()
        assert len(lines) <= test_max, f"{path.relative_to(ROOT)} has {len(lines)} lines; max is {test_max}"


def test_state_module_reexports_canonical_fsm_state():
    assert State is fsm.State


def test_architecture_mermaid_matches_fsm_transitions():
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    for (source, event), target in fsm.TRANSITIONS.items():
        expected = f"{source.value} --> {target.value}: {event.value}"
        assert expected in architecture


def test_no_import_cycles_in_top_level_quikode_modules():
    modules = {}
    for path in (ROOT / "quikode").glob("*.py"):
        if path.name == "__init__.py":
            continue
        module = f"quikode.{path.stem}"
        imports: set[str] = set()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                imports.add(f"quikode.{node.module.split('.')[0]}")
        modules[module] = {
            m for m in imports if m in modules or (ROOT / "quikode" / f"{m.rsplit('.', 1)[1]}.py").exists()
        }

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, stack: list[str]) -> None:
        if module in visited:
            return
        if module in visiting:
            cycle = " -> ".join([*stack, module])
            raise AssertionError(f"import cycle detected: {cycle}")
        visiting.add(module)
        for dep in modules.get(module, set()):
            visit(dep, [*stack, module])
        visiting.remove(module)
        visited.add(module)

    for module in sorted(modules):
        visit(module, [])


def test_reinstall_script_runs_ty_over_source_and_tests():
    script = (ROOT / "scripts" / "reinstall.sh").read_text()
    assert "uv run ty check quikode tests" in script


def test_planner_modules_have_no_prose_parsing_residue():
    """Plan 38 PR-B.4: the planner / fixup-planner / merge-planner drivers
    consume already-validated wire-schema pydantic instances. The
    heuristic JSON-extract surface (`extract_json`, `_FENCED_JSON_RE`,
    `parse_planner_output`, `parse_fixup_planner_output`,
    `_JSON_OBJECT_RE`) must not appear in the four targeted modules."""
    targeted = [
        ROOT / "quikode" / "subtask_schema.py",
        ROOT / "quikode" / "workers" / "planner_driver.py",
        ROOT / "quikode" / "workers" / "fixup_coverage.py",
        ROOT / "quikode" / "workers" / "merge_node_worker.py",
    ]
    banned = (
        "extract_json(",
        "_FENCED_JSON_RE",
        "_JSON_OBJECT_RE",
        "first_balanced_object",
        "parse_planner_output",
        "parse_fixup_planner_output",
    )
    for path in targeted:
        text = path.read_text()
        for term in banned:
            assert term not in text, (
                f"{path.relative_to(ROOT)} still references prose-parsing surface {term!r}"
            )
