"""Plan 38 PR-B.7: package marker.

The prior CLI shims (`Agent.run` against `OpencodeAgent` / `CodexAgent` /
`ClaudeAgent`) and the `build_agent(role)` factory were retired together
with the `AgentRole` config struct and the prose-parsing call sites.
The live transport surface is now `quikode.agents.json_protocol` +
the per-CLI JSON shims (`json_codex_direct`, `json_codex_litellm`,
`json_claude`); roles bind to schemas via `quikode.agent_registry`.
"""
