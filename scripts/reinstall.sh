#!/usr/bin/env bash
# Reinstall the quikode CLI tool from the source checkout so a running daemon
# picks up Python-side changes on its next restart.
#
# Why this script exists: `quikode daemon start` runs the version installed via
# `uv tool install`, NOT the source checkout. Editing `quikode/*.py` in the
# repo has zero effect on a running daemon until a reinstall happens.
# Prompt templates (`prompts/*.md`) were the same gotcha until `pyproject.toml`
# started bundling them via `tool.hatch.build.targets.wheel.force-include`.
#
# Workflow:
#
#   1. Edit source.
#   2. Run tests + lint + format (skipped if --skip-tests).
#   3. `uv tool install --reinstall .` so the installed `quikode` matches.
#   4. (Optional) `quikode daemon stop && quikode daemon start --detach ...`
#      from the workspace dir — this script does NOT do that, since restart
#      is destructive (orphan-recovers in-flight tasks).
#
# Usage:
#
#   ./scripts/reinstall.sh             # tests + lint + format + reinstall
#   ./scripts/reinstall.sh --skip-tests # reinstall only (CI, hot-fix paths)
#
# Run from the repo root.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f pyproject.toml ]]; then
    echo "error: must run from the quikode repo root (no pyproject.toml found)" >&2
    exit 1
fi

SKIP_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        --help|-h)
            sed -n '2,32p' "$0"
            exit 0
            ;;
        *)
            echo "unknown flag: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

if [[ "$SKIP_TESTS" -eq 0 ]]; then
    echo "==> ruff check"
    ruff check quikode/ tests/
    echo "==> ruff format --check"
    ruff format --check quikode/ tests/
    echo "==> pytest"
    python -m pytest tests/ -q
fi

echo "==> uv tool install --reinstall ."
uv tool install --reinstall . > /tmp/quikode-reinstall.log 2>&1
tail -3 /tmp/quikode-reinstall.log

echo
echo "✓ reinstalled — running daemon will see changes after the next restart."
echo "  cd <workspace> && quikode daemon stop && quikode daemon start --detach --max-parallel N --retry-failed"
