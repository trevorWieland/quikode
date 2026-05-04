from ..config import AgentRole
from .base import Agent, AgentResult
from .claude import ClaudeAgent
from .codex import CodexAgent
from .opencode import OpencodeAgent

__all__ = ["Agent", "AgentResult", "ClaudeAgent", "CodexAgent", "OpencodeAgent", "build_agent"]


def build_agent(role: AgentRole) -> Agent:
    if role.cli == "claude":
        return ClaudeAgent(model=role.model, extra_args=role.extra_args)
    if role.cli == "codex":
        return CodexAgent(model=role.model, extra_args=role.extra_args)
    if role.cli == "opencode":
        return OpencodeAgent(model=role.model, extra_args=role.extra_args)
    raise ValueError(f"unknown agent cli: {role.cli}")
