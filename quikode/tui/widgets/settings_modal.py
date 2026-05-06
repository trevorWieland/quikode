"""Settings modal — driven by `Config.model_json_schema()`.

Renders a form for the high-traffic config knobs. On Apply, builds a new
Config (which validates via pydantic) and writes back to .quikode/config.toml.

For v1 we expose:
- max_parallel
- triage_budget_per_phase
- stacking_strategy (enum dropdown)
- conflict_auto_resolve (toggle)

For everything else, /config opens the TOML in $EDITOR.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, cast

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static, Switch

from quikode.config import Config, StackingStrategy

# Fields surfaced in the modal. Each entry: (field name, widget kind, optional choices).
# v1 deliberately doesn't render every Config field — only the high-traffic ones.
_MODAL_FIELDS: list[tuple[str, str]] = [
    ("max_parallel", "int"),
    ("triage_budget_per_phase", "int"),
    ("stall_warn_seconds", "int"),
    ("stacking_strategy", "enum"),
    ("conflict_auto_resolve", "bool"),
    ("max_parallel_auto", "bool"),
]


def _field_meta(field: str) -> dict[str, Any]:
    """Pull pydantic Field metadata (description, bounds) for a Config field."""
    schema = Config.model_json_schema()["properties"]
    return schema.get(field, {})


class SettingsModal(ModalScreen[bool]):
    """Schema-driven settings editor."""

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
    }

    #settings-box {
        width: 90;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #settings-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .settings-row {
        height: 3;
        layout: horizontal;
    }

    .settings-row Static {
        width: 1fr;
    }

    .settings-row Input,
    .settings-row Select,
    .settings-row Switch {
        width: 30;
    }

    #settings-error {
        color: $error;
        margin: 1 0;
    }

    #settings-buttons {
        height: 3;
        margin-top: 1;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        cfg: Config,
        config_toml_path: Path,
        *,
        on_apply: Callable[[Config], None] | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.config_toml_path = config_toml_path
        self._on_apply = on_apply

    def compose(self) -> ComposeResult:
        with Container(id="settings-box"):
            yield Static("⚙ quikode settings", id="settings-title")
            with Vertical():
                for name, kind in _MODAL_FIELDS:
                    meta = _field_meta(name)
                    label = f"{name}\n[dim]{meta.get('description', '')}[/]"
                    cur = getattr(self.cfg, name)
                    with Horizontal(classes="settings-row"):
                        yield Static(label)
                        yield self._field_widget(name, kind, cur, meta)
            yield Static("", id="settings-error")
            with Horizontal(id="settings-buttons"):
                yield Button("Apply", variant="primary", id="settings-apply")
                yield Button("Apply + Restart Orchestrator", id="settings-apply-restart")
                yield Button("Cancel", id="settings-cancel")

    @staticmethod
    def _field_widget(name: str, kind: str, cur: Any, meta: dict[str, Any]) -> Any:
        if kind == "int":
            mn = meta.get("minimum")
            mx = meta.get("maximum")
            placeholder = f"{mn}–{mx}" if mn is not None and mx is not None else "int"
            return Input(value=str(cur), placeholder=placeholder, id=f"field-{name}")
        if kind == "bool":
            return Switch(value=bool(cur), id=f"field-{name}")
        if kind == "enum":
            options = [(s.value, s.value) for s in StackingStrategy]
            return Select(options=options, value=str(cur), id=f"field-{name}")
        return Input(value=str(cur), id=f"field-{name}")

    # ----- actions -----

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-cancel":
            self.dismiss(False)
            return
        restart = event.button.id == "settings-apply-restart"
        self._apply(restart=restart)

    def _apply(self, *, restart: bool) -> None:
        # Collect values
        update: dict[str, Any] = {}
        for name, kind in _MODAL_FIELDS:
            widget = self.query_one(f"#field-{name}")
            if kind == "int":
                input_widget = cast(Input, widget)
                try:
                    update[name] = int(input_widget.value)
                except (ValueError, TypeError):
                    self._show_error(f"{name}: must be an integer")
                    return
            elif kind == "bool":
                update[name] = bool(cast(Switch, widget).value)
            elif kind == "enum":
                v = cast(Select, widget).value
                if v in (None, Select.BLANK):
                    self._show_error(f"{name}: pick a value")
                    return
                update[name] = v
        # Validate via pydantic by re-constructing Config from current values + overrides.
        # Use model_dump(mode="python") so enum values stay as enums (skip the
        # serializer warning).
        try:
            base = self.cfg.model_dump(mode="python")
            base.update(update)
            new_cfg = Config.model_validate(base)
        except Exception as e:  # ValidationError + others
            self._show_error(str(e))
            return
        # Persist (best effort — we re-write only the keys the modal handles).
        try:
            _persist_overrides(self.config_toml_path, update)
        except OSError as e:
            self._show_error(f"failed to write {self.config_toml_path}: {e}")
            return
        if self._on_apply is not None:
            self._on_apply(new_cfg)
        # Surface "restart" intent to the caller via the dismiss return value:
        # True = applied, restart wanted. Caller decides what to do.
        self.dismiss(restart)

    def _show_error(self, msg: str) -> None:
        # Pydantic error messages contain `[type=...]` which textual interprets
        # as markup tags. Render the message as plain text by escaping brackets.
        safe = escape(str(msg))
        self.query_one("#settings-error", Static).update(f"[red]✗ {safe}[/]")


# Maps Config field name → (toml_section, toml_key). When toml_section is
# None the override goes at the top level. Mirrors what `load_config` reads.
_TOML_SCHEMA: dict[str, tuple[str | None, str]] = {
    "max_parallel": (None, "max_parallel"),
    "triage_budget_per_phase": (None, "triage_budget_per_phase"),
    "stall_warn_seconds": (None, "stall_warn_seconds"),
    "max_parallel_auto": ("resources", "max_parallel_auto"),
    "conflict_auto_resolve": ("conflicts", "auto_resolve"),
    "stacking_strategy": ("stacking", "strategy"),
}


def _persist_overrides(toml_path: Path, overrides: dict[str, Any]) -> None:
    """Replace/append config values in TOML. Handles top-level keys + the
    sectioned keys exposed by the v1 settings modal (resources, conflicts,
    intent, stacking)."""
    if not toml_path.exists():
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text("# quikode config\n")
    text = toml_path.read_text()
    for field, value in overrides.items():
        section, key = _TOML_SCHEMA.get(field, (None, field))
        text = _set_toml_key(text, section, key, value)
    toml_path.write_text(text if text.endswith("\n") else text + "\n")


def _set_toml_key(text: str, section: str | None, key: str, value: Any) -> str:
    """Set `key = value` inside the given section (or top-level if None).

    Handles three cases:
    1. Section + key both already present → replace the value line
    2. Section present but key missing → insert key after section header
    3. Section missing entirely → append `[section]\\n key = value` at end of file
    For top-level (section is None), behavior matches case 1/2 against the
    pre-section header preamble.
    """
    formatted = _format_toml_value(value)
    lines = text.splitlines()
    if section is None:
        # Top-level — operate on lines before the first [section] header.
        sec_idx = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
        for i in range(sec_idx):
            ln = lines[i].strip()
            if ln.startswith((f"{key} ", f"{key}=")):
                lines[i] = f"{key} = {formatted}"
                return "\n".join(lines)
        lines.insert(sec_idx, f"{key} = {formatted}")
        return "\n".join(lines)
    # Sectioned write
    header = f"[{section}]"
    sec_start = next((i for i, ln in enumerate(lines) if ln.strip() == header), -1)
    if sec_start < 0:
        # Section absent — append it at end of file.
        sep = "" if not lines or lines[-1].strip() == "" else "\n"
        lines.extend([sep + header, f"{key} = {formatted}"])
        return "\n".join(lines)
    # Find end of section (next [header] or EOF)
    sec_end = next(
        (i for i in range(sec_start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    for i in range(sec_start + 1, sec_end):
        ln = lines[i].strip()
        if ln.startswith((f"{key} ", f"{key}=")):
            lines[i] = f"{key} = {formatted}"
            return "\n".join(lines)
    # Key absent within section — insert just after the header.
    lines.insert(sec_start + 1, f"{key} = {formatted}")
    return "\n".join(lines)


def _format_toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return f'"{v}"'
