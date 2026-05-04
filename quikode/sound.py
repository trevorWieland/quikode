"""Trivial notification sound. Best-effort."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ding() -> None:
    """Try a few common WSL/Linux paths to play a short sound. Silent on failure."""
    for player, args in [
        ("paplay", []),
        ("aplay", []),
        ("play", []),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ]:
        if not shutil.which(player):
            continue
        for sound in [
            "/usr/share/sounds/freedesktop/stereo/complete.oga",
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
            "/usr/share/sounds/sound-icons/glass-water-1.wav",
        ]:
            if Path(sound).exists():
                subprocess.run([player, *args, sound], check=False, capture_output=True, timeout=5)
                return
    # final fallback: terminal bell
    print("\a", end="", flush=True)
