#!/usr/bin/env bash
# Build a quikode dev image. Pass a flavor name and optional tag.
#
# Usage:
#   docker/build.sh tanren     # builds quikode-tanren-dev:latest from Dockerfile
#   docker/build.sh python     # builds quikode-python-dev:latest from Dockerfile.python
#   docker/build.sh python my-tag:1.0
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FLAVOR="${1:-tanren}"
case "$FLAVOR" in
  tanren) DOCKERFILE="$HERE/Dockerfile";        DEFAULT_TAG="quikode-tanren-dev:latest" ;;
  python) DOCKERFILE="$HERE/Dockerfile.python"; DEFAULT_TAG="quikode-python-dev:latest" ;;
  *) echo "unknown flavor: $FLAVOR (expected tanren|python)" >&2; exit 2 ;;
esac
TAG="${2:-$DEFAULT_TAG}"
docker build -t "$TAG" -f "$DOCKERFILE" "$HERE"
echo "==> built $TAG"
