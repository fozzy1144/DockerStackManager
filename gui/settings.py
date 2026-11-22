"""Window state remembered between runs: size, position, maximised, last host.

Kept apart from ``hosts.json`` because it is disposable — a corrupt or missing
file costs a default window size and nothing else — and because it changes on
every close, while the host list should only be rewritten when it changes.
"""

import ctypes
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from core.credentials import config_dir

FILENAME = "ui.json"

_GEOMETRY = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")
_MIN_VISIBLE = 120
"""Pixels of title bar that must land on a screen for a saved position to be used."""


def settings_path() -> Path:
    return config_dir() / FILENAME


def load(path: Optional[Path] = None) -> dict:
    """The saved settings, or ``{}`` if there are none or they cannot be read."""
    try:
        with open(path or settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, path: Optional[Path] = None) -> None:
    """Write ``data`` atomically. Failure is silent: this is a convenience."""
    target = path or settings_path()
    temp = target.with_suffix(".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(temp, target)
    except OSError:
        pass


def parse_geometry(geometry: str) -> Optional[tuple[int, int, int, int]]:
    """``"WxH+X+Y"`` as ``(w, h, x, y)``, or ``None`` if it is not one."""
    match = _GEOMETRY.match((geometry or "").strip())
    if not match:
        return None
    width, height = int(match[1]), int(match[2])
    x, y = int(match[3].replace("+-", "-")), int(match[4].replace("+-", "-"))
    return width, height, x, y


def usable_geometry(geometry: str, screen: tuple[int, int, int, int],
                    minimum: tuple[int, int] = (640, 480)) -> Optional[str]:
    """``geometry`` if it would put the window somewhere visible, else ``None``.

    ``screen`` is the virtual desktop as ``(x, y, width, height)``. A monitor that
    was unplugged since the last run must not leave the window off-screen, so the
    top edge — where the title bar is — has to overlap the desktop.
    """
    parsed = parse_geometry(geometry)
    if parsed is None:
        return None
    width, height, x, y = parsed
    if width < minimum[0] or height < minimum[1]:
        return None
    sx, sy, sw, sh = screen
    overlap = min(x + width, sx + sw) - max(x, sx)
    if overlap < _MIN_VISIBLE or not (sy <= y <= sy + sh - 40):
        return None
    # Always "+": in Tk, "-100" measures from the right edge; "+-100" is x = -100.
    return f"{width}x{height}+{x}+{y}"


def virtual_screen(window) -> tuple[int, int, int, int]:
    """The whole desktop across monitors (Windows), or the screen Tk reports."""
    if sys.platform.startswith("win"):
        try:
            metric = ctypes.windll.user32.GetSystemMetrics
            # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
            box = (metric(76), metric(77), metric(78), metric(79))
            if box[2] > 0 and box[3] > 0:
                return box
        except (AttributeError, OSError):
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()
