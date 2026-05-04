#!/usr/bin/env bash
# quikode entrypoint — copies read-only host auth into writable container locations,
# then execs the given command (or sleeps if none).
#
# Host auth is bind-mounted at /host-auth/{claude,codex,opencode-data,opencode-config}.
# We copy to $HOME so each agent CLI can mutate its own session/history db without
# stepping on parallel containers or polluting the host.
set -euo pipefail

HOME_DIR="${HOME:-/home/dev}"

copy_auth() {
    local src="$1" dst="$2"
    if [[ -d "$src" ]]; then
        mkdir -p "$dst"
        # -L follows symlinks, --no-preserve=ownership avoids EPERM with remapped uid
        cp -RL --no-preserve=ownership "$src/." "$dst/" 2>/dev/null || true
        chmod -R u+rwX "$dst" 2>/dev/null || true
    fi
}

copy_auth /host-auth/claude          "$HOME_DIR/.claude"
copy_auth /host-auth/codex           "$HOME_DIR/.codex"
copy_auth /host-auth/opencode-data   "$HOME_DIR/.local/share/opencode"
copy_auth /host-auth/opencode-config "$HOME_DIR/.config/opencode"

# claude-code stores its OAuth state in ~/.claude.json (a file at $HOME root,
# NOT inside ~/.claude/). Copy it if mounted.
if [[ -f /host-auth/claude.json ]]; then
    cp -L --no-preserve=ownership /host-auth/claude.json "$HOME_DIR/.claude.json"
    chmod u+rw "$HOME_DIR/.claude.json" 2>/dev/null || true
fi

# Configure git author so commits inside the container have a valid identity.
git config --global user.email "${QK_GIT_EMAIL:-quikode@localhost}"
git config --global user.name  "${QK_GIT_NAME:-quikode}"
git config --global init.defaultBranch main
# Trust the workspace and any parent repo path mounted in (since git's safe-directory
# check trips on uid mismatches between host and container).
git config --global --add safe.directory '*'

# Configure git to use GITHUB_TOKEN for HTTPS auth. Two layers:
#   1) gh auth setup-git installs `gh` as a credential helper (preferred path —
#      uses the same token gh is using).
#   2) Fallback: a static credential.helper that echoes the env token directly.
#      This handles edge cases where setup-git doesn't take.
mkdir -p "$HOME_DIR/.config/gh" 2>/dev/null || true
if [[ -n "${GITHUB_TOKEN:-}${GH_TOKEN:-}" ]]; then
    gh auth setup-git --hostname github.com 2>/dev/null || true
    git config --global credential.helper 'store --file=/tmp/.git-credentials'
    cat > /tmp/.git-credentials <<CREDS
https://x-access-token:${GITHUB_TOKEN:-${GH_TOKEN}}@github.com
CREDS
    chmod 600 /tmp/.git-credentials
fi

# Sentinel: orchestrator polls this file to know the auth copy is complete.
touch /tmp/qk-ready

exec "$@"
