"""Reusable confirm modal — used by destructive slash commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """Yes/No prompt. dismiss(True) on confirm, dismiss(False) on cancel."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }

    #confirm-box {
        width: 60;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #confirm-message {
        margin-bottom: 1;
    }

    #confirm-buttons {
        height: 3;
        width: 100%;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, message: str, *, on_done: Callable[[bool], None] | None = None) -> None:
        super().__init__()
        self.message = message
        self._on_done = on_done

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(self.message, id="confirm-message")
            with Container(id="confirm-buttons"):
                yield Button("Confirm (y)", variant="error", id="confirm-yes")
                yield Button("Cancel (n)", id="confirm-no")

    def action_confirm(self) -> None:
        self._finish(True)

    def action_cancel(self) -> None:
        self._finish(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._finish(event.button.id == "confirm-yes")

    def _finish(self, result: bool) -> None:
        if self._on_done is not None:
            self._on_done(result)
        self.dismiss(result)
