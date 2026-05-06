# Roadmap

Active work only:

- Complete the mechanical worker split into `quikode/workers/*`.
- Complete the mechanical orchestrator split into `quikode/orchestration/*`.
- Complete the CLI split into command modules.
- Add fake project, fake agent, and fake GitHub providers for full-loop tests.
- Add local-git rebase and conflict scenario tests without live GitHub.
- Add architecture gates for file length, banned active vocabulary, and import
  cycles.
- Convert remaining worker/orchestrator state writes to canonical FSM events.

Historical design notes live under `docs/archive/`.
