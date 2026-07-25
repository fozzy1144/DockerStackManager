"""Shared colours, so a severity means the same thing in every panel.

The log panel, the lint findings list, and the editor's status bar all speak in
levels. Keeping one table here is what stops "warning" being amber in one place
and orange in another.
"""

ACCENT_PURPLE = "#7B5EA7"
ACCENT_PURPLE_HOVER = "#6A4D96"
ACCENT_BLUE = "#2B5278"
ACCENT_BLUE_HOVER = "#1E3D5C"
ACCENT_TEAL = "#2A7B7B"
ACCENT_TEAL_HOVER = "#1F5C5C"
DANGER = "#883333"
DANGER_HOVER = "#AA4444"
SELECTED = "#1E6B9E"

#: Severity accents. Both spellings of each level are present because the log
#: uses "warn"/"success" while :mod:`core.compose` uses "warning"/"info".
LEVEL_ACCENTS = {
    "error": "#FF7F7F",
    "warning": "#FFD700",
    "warn": "#FFD700",
    "info": "#A0D8EF",
    "success": "#90EE90",
    "saved": "#90EE90",
}

#: Container and stack states.
STATE_COLORS = {
    "running": "#4CAF50",
    "healthy": "#4CAF50",
    "partial": "#FFA500",
    "starting": "#FFA500",
    "restarting": "#FFA500",
    "unhealthy": "#FF7F7F",
    "paused": "#AAAAAA",
    "stopped": "#888888",
    "exited": "#888888",
    "created": "#AAAAAA",
    "unknown": "#AAAAAA",
}

MUTED = "gray60"
SUBTLE = "gray70"


def state_color(state: str) -> str:
    """Colour for a container or stack state, falling back to neutral grey."""
    return STATE_COLORS.get((state or "").lower(), STATE_COLORS["unknown"])


def level_color(level: str, default: str = SUBTLE) -> str:
    return LEVEL_ACCENTS.get(level, default)
