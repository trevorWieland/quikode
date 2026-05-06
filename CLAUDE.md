# Quikode Contributor Notes

Quikode uses `quikode.fsm` as the single authority for task states and task events. Runtime state changes should call `Store.apply_event(...)`; fresh workspace seeding uses `Store.seed_merged_node(...)`.

Run the local validation ladder before publishing changes:

```bash
uv run ruff check quikode tests
uv run ruff format --check quikode tests
uv run ty check quikode tests
uv run pytest tests/ -q
```

Do not add skipped tests, inline type or lint suppressions, or hidden alternate runtime paths for planner/checker/PR/scheduler failures. Model those failures explicitly and make the task state visible.
