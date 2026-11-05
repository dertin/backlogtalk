"""Read desktop-managed shortcuts without changing system settings."""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_CUSTOM = _SCHEMA + ".custom-keybinding"
_PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def _read_value(schema: str, key: str):
    result = subprocess.run(
        ["gsettings", "get", schema, key],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2,
        check=True,
    )
    value = result.stdout.strip()
    if value == "@as []":
        return []
    return ast.literal_eval(value)


def format_binding(binding: str) -> str:
    modifiers = {
        "control": "Ctrl",
        "primary": "Ctrl",
        "alt": "Alt",
        "mod1": "Alt",
        "shift": "Shift",
        "super": "Super",
        "meta": "Meta",
        "hyper": "Hyper",
    }
    parts = []
    while binding.startswith("<"):
        end = binding.find(">")
        if end < 0:
            return ""
        modifier = modifiers.get(binding[1:end].lower())
        if modifier is None:
            return ""
        if modifier not in parts:
            parts.append(modifier)
        binding = binding[end + 1 :]
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+", binding):
        return ""
    parts.append(binding.upper() if len(binding) == 1 else binding)
    return "+".join(parts)


def gnome_global_shortcuts(root: Path) -> dict[str, list[str]] | None:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if not sys.platform.startswith("linux") or not any(x in desktop for x in ("gnome", "ubuntu")):
        return None
    runner = str(root / "scripts" / "run.sh")
    found = {"mic": [], "meeting": [], "open": []}
    try:
        paths = _read_value(_SCHEMA, "custom-keybindings")
        if not isinstance(paths, list):
            return None
        for path in paths:
            if not isinstance(path, str) or not re.fullmatch(
                re.escape(_PREFIX) + r"[A-Za-z0-9_-]+/", path
            ):
                continue
            schema = f"{_CUSTOM}:{path}"
            command = _read_value(schema, "command")
            binding = _read_value(schema, "binding")
            if not isinstance(command, str) or not isinstance(binding, str):
                continue
            try:
                args = shlex.split(command)
            except ValueError:
                continue
            if not args or args[0] != runner:
                continue
            if "--capture-system" in args[1:]:
                action = "meeting"
            elif "--capture" in args[1:]:
                action = "mic"
            elif "--open-history" in args[1:] or len(args) == 1:
                action = "open"
            else:
                continue
            display = format_binding(binding)
            if display and display not in found[action]:
                found[action].append(display)
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError, TypeError):
        return None
    return found
