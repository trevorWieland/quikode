# quikode dev image — for tanren and similar rust+react+pg projects.
#
# We install the full toolchain (rust, node, pnpm, just, mold, gh, agent CLIs)
# but DO NOT bake any auth into the image. Auth dirs are bind-mounted at run time.
#
# Build:
#   docker build -t quikode-tanren-dev:latest -f docker/Dockerfile .

FROM debian:trixie-slim

ARG RUST_VERSION=1.94.1
ARG NODE_MAJOR=24
ARG PNPM_VERSION=10.19.0

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/home/dev/.cargo/bin:/home/dev/.local/bin:/home/dev/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin

# ---- system packages ----------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git build-essential pkg-config libssl-dev \
        cmake mold clang lld \
        bash bash-completion \
        sudo gnupg jq \
        passwd procps \
        unzip xz-utils \
        sqlite3 \
        postgresql-client \
        ripgrep fd-find \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# ---- gh CLI -------------------------------------------------------------------
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ---- create dev user (uid/gid will be remapped at run via --user) -------------
RUN /usr/sbin/useradd -m -s /bin/bash -u 1000 dev \
    && echo "dev ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
USER dev
WORKDIR /home/dev

# ---- rust ---------------------------------------------------------------------
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain ${RUST_VERSION} \
                   --profile minimal \
                   -c rustfmt -c clippy -c llvm-tools-preview

# ---- cargo tools (use binstall to skip compilation) ---------------------------
RUN curl -L --proto '=https' --tlsv1.2 -sSf \
      https://raw.githubusercontent.com/cargo-bins/cargo-binstall/main/install-from-binstall-release.sh \
      | bash
RUN cargo binstall --no-confirm --locked \
        just cargo-deny cargo-llvm-cov cargo-machete \
        cargo-mutants cargo-edit cargo-hack \
        taplo-cli lychee rumdl sccache

# ---- node + pnpm via nvm ------------------------------------------------------
ENV NVM_DIR=/home/dev/.nvm
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install ${NODE_MAJOR} \
    && nvm alias default ${NODE_MAJOR} \
    && ln -sfn "$NVM_DIR/versions/node/$(nvm version default)" "$NVM_DIR/versions/node/current" \
    && corepack enable \
    && corepack prepare pnpm@${PNPM_VERSION} --activate

# ---- agent CLIs ---------------------------------------------------------------
# claude-code (npm global)
RUN . "$NVM_DIR/nvm.sh" && npm install -g @anthropic-ai/claude-code

# codex (npm global)
RUN . "$NVM_DIR/nvm.sh" && npm install -g @openai/codex

# opencode (official installer)
RUN curl -fsSL https://opencode.ai/install | bash

# ccusage variants — pre-install so the first agent call doesn't pay the
# ~30s npx cold-cache download. quikode/agents/ccusage.py shells out to
# `npx -y <pkg>@latest session --json` after each agent invocation to
# capture token + cost data uniformly across the three CLIs.
RUN . "$NVM_DIR/nvm.sh" && npm install -g ccusage @ccusage/codex @ccusage/opencode

# lefthook — pre-commit hook runner used by tanren's lefthook.yml. Installed
# via npm since the official cargo crate isn't on cargo-binstall and the
# project's docs list npm as a supported install path. The worker invokes
# `lefthook run pre-commit --files-from-stdin` for per-subtask gating.
RUN . "$NVM_DIR/nvm.sh" && npm install -g lefthook

# ---- shell niceties -----------------------------------------------------------
# Make all CLIs available in BOTH login and non-login shells regardless of USER.
USER root
RUN cat > /etc/profile.d/quikode.sh <<'EOF'
export NVM_DIR=/home/dev/.nvm
export PATH=/home/dev/.cargo/bin:/home/dev/.local/bin:/home/dev/.opencode/bin:$NVM_DIR/versions/node/current/bin:$PATH
EOF
RUN chmod 0644 /etc/profile.d/quikode.sh
USER dev
RUN echo '. /etc/profile.d/quikode.sh' >> /home/dev/.bashrc

USER root
COPY entrypoint.sh /usr/local/bin/quikode-entrypoint
RUN chmod 0755 /usr/local/bin/quikode-entrypoint
# Pre-create cargo target + sccache dirs as root, then make them world-writable
# so the remapped user inside the container can use them.
RUN mkdir -p /home/dev/cargo-target /home/dev/.cache /sccache \
    && chmod -R 0777 /home/dev/cargo-target /home/dev/.cache /sccache
USER dev

ENTRYPOINT ["/usr/local/bin/quikode-entrypoint"]

# Wire sccache as the rustc wrapper. Cache lives at /sccache (bind-mounted from host
# at run time so it survives container teardown and is shared across parallel tasks).
ENV RUSTC_WRAPPER=sccache \
    SCCACHE_DIR=/sccache \
    SCCACHE_CACHE_SIZE=20G

CMD ["sleep", "infinity"]
