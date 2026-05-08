"""Plan 35 PR-A: `qk standards seed` — copy the seed standards-profile
tree into an operator's repo so they can fork-and-edit.

The seed lives at `quikode/standards_profiles_seed/`. The command
copies that directory into a target path (default `./profiles/`),
preserving the per-profile/per-category structure. Existing target
paths are protected: the command refuses to overwrite without
`--force`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .cli_context import app, console, typer

_SEED_ROOT = Path(__file__).resolve().parent / "standards_profiles_seed"


standards_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Standards-profile management commands (plan 35).",
)
app.add_typer(standards_app, name="standards")


@standards_app.command("seed")
def seed_standards(
    to: Path = typer.Option(
        Path("profiles"),
        "--to",
        help="Target directory to copy the seed profile tree into. "
        "Defaults to `./profiles` in the current working directory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the target if it already exists.",
    ),
) -> None:
    """Copy the bundled standards-profile seed tree into the operator's repo."""
    if not _SEED_ROOT.exists() or not _SEED_ROOT.is_dir():
        console.print(
            f"[red]seed root missing or not a directory: {_SEED_ROOT}[/]\n"
            "this is a quikode packaging bug; please report."
        )
        raise typer.Exit(1)
    target = to.resolve()
    if target.exists():
        if not force:
            console.print(f"[red]target {target} already exists[/]; pass --force to overwrite.")
            raise typer.Exit(1)
        shutil.rmtree(target)
    shutil.copytree(_SEED_ROOT, target)
    profile_count = sum(1 for _ in target.iterdir() if _.is_dir())
    md_count = sum(1 for _ in target.rglob("*.md") if _.is_file())
    console.print(f"[green]seeded {profile_count} profile(s), {md_count} doc(s) into {target}[/]")
