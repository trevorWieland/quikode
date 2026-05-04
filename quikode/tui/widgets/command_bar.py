"""Command bar — slash command input with fuzzy autocomplete + free-text hint.

The bar dispatches slash-prefixed input to `controllers.command_dispatch`.
Free text (no leading slash) is reserved for a future supervisory agent;
v1 just shows the hint that v2.5+ will route it elsewhere.

Autocomplete UX:
- Typing `/` opens a popover showing all commands.
- As more characters are typed, the list fuzzy-filters by subject substring.
- Tab autocompletes to the highlighted entry.
- Enter executes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Input, Static


class CommandSuggestionList:
    """Plain non-widget helper: rank slash commands by fuzzy match."""

    def __init__(self, catalog: dict[str, str]) -> None:
        # catalog: command name (without leading slash) -> one-line description
        self.catalog = catalog

    def filter(self, query: str) -> list[tuple[str, str]]:
        """Return [(cmd, description), ...] best-match first.

        Matching: case-insensitive substring of the command name. Fall back
        to substring of description when nothing matches the name.
        """
        q = query.lstrip("/").lower()
        if not q:
            return list(self.catalog.items())
        # Score 0 = prefix match, 1 = substring of name, 2 = substring of description.
        scored: list[tuple[int, int, str, str]] = []
        for name, desc in self.catalog.items():
            n = name.lower()
            d = desc.lower()
            if n.startswith(q):
                scored.append((0, len(name) - len(q), name, desc))
            elif q in n:
                scored.append((1, n.index(q), name, desc))
            elif q in d:
                scored.append((2, d.index(q), name, desc))
        scored.sort()
        return [(name, desc) for _, _, name, desc in scored]


class CommandBar(Container):
    """Combined input + suggestions popover.

    Step 1 deliverable: input present, free-text hint shown when typed text
    doesn't start with `/`. Full autocomplete arrives in step 5.
    """

    DEFAULT_CSS = ""

    def __init__(
        self,
        catalog: dict[str, str],
        on_submit: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.suggestions = CommandSuggestionList(catalog)
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/command — Tab to complete · ? for help", id="command-input")
        yield Static(self._hint(""), id="command-hint")

    def _hint(self, value: str) -> str:
        if not value:
            return "[dim]start with / to invoke a command · ↑↓ to nav tasks · ? for help[/]"
        if not value.startswith("/"):
            return "[yellow]free text not yet supported (reserved for future supervisory agent)[/]"
        ranked: Iterable[tuple[str, str]] = self.suggestions.filter(value)
        top = list(ranked)[:5]
        if not top:
            return "[red]no matching command[/]"
        return " · ".join(f"[b]/{name}[/] [dim]{desc}[/]" for name, desc in top)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#command-hint", Static).update(self._hint(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        self.query_one("#command-hint", Static).update(self._hint(""))
        if value:
            self._on_submit(value)

    def on_key(self, event: events.Key) -> None:
        # Tab autocompletes to the top-ranked suggestion.
        if event.key == "tab":
            inp = self.query_one("#command-input", Input)
            ranked = self.suggestions.filter(inp.value)
            if ranked:
                inp.value = "/" + ranked[0][0] + " "
                inp.cursor_position = len(inp.value)
                event.stop()
