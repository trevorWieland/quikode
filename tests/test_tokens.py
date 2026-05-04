"""parse_tokens covers all three agents' output formats."""

from __future__ import annotations

from quikode.agents.base import parse_tokens


def test_codex_tokens_used_format_in_stderr():
    stderr = """OpenAI Codex v0.128.0 (research preview)
--------
session id: foo
--------
user
say hi

codex
Hello there!
tokens used
6,201
"""
    assert parse_tokens("", stderr) == 6201


def test_no_tokens_returns_none():
    assert parse_tokens("", "no token info here") is None
    assert parse_tokens("", "") is None


def test_generic_total_tokens_pattern():
    s = "Some output\nTotal tokens: 1234\nmore text"
    assert parse_tokens(s, "") == 1234


def test_stderr_takes_precedence():
    stderr = "tokens used\n100\n"
    stdout = "Total tokens: 999\n"
    # stderr is checked first (codex's preferred location)
    assert parse_tokens(stdout, stderr) == 100
