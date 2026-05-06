"""Small helpers for extracting JSON objects from agent text."""

from __future__ import annotations


def first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    end = _balanced_object_end(text, start)
    return text[start : end + 1] if end is not None else None


def _balanced_object_end(text: str, start: int) -> int | None:
    depth = 0
    in_str = False
    escaped = False
    for index, char in enumerate(text[start:], start):
        if in_str:
            escaped, in_str = _advance_string_scan(char, escaped, in_str)
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _advance_string_scan(char: str, escaped: bool, in_str: bool) -> tuple[bool, bool]:
    if escaped:
        return False, in_str
    if char == "\\":
        return True, in_str
    if char == '"':
        return False, False
    return False, in_str
