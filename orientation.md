# Orientation

Start here:

- `README.md`: install, quick start, command map.
- `docs/architecture.md`: FSM, store, profiles, recovery.
- `docs/runbook-operations.md`: daily operation.
- `docs/runbook-incident-response.md`: failure handling.
- `docs/profiles/tanren.md`: Tanren-specific profile notes.
- `docs/roadmap.md`: remaining active work.

Validation:

```bash
uv run ruff check quikode tests
uv run ruff format --check quikode tests
uv run ty check quikode tests
uv run pytest tests/ -q
```
