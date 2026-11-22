"""The design system: colours, type, and the handful of widget recipes every window uses.

One module owns the look so that a severity, a surface, or a button means the same
thing in every panel. Windows build their widgets through the factories here
(:func:`button`, :func:`card`, :func:`pill`…) rather than passing colours by hand;
that is what keeps "secondary button" from drifting into five slightly different
greys over time.

:func:`apply` must run once before the root window is created — it rewrites
CustomTkinter's theme table, which widgets read at construction — and
:func:`init_fonts` once the root exists, because Tk can only list installed font
families with a live interpreter.
"""

import base64
import ctypes
import math
import struct
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import zlib
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

# ──────────────────────────────────────────────────────────────────────────────
# Palette
# ──────────────────────────────────────────────────────────────────────────────
# A cool, near-black neutral ramp with one accent. Elevation is expressed by
# stepping up the ramp, not by shadows — Tk has none — and every surface above
# the window background carries a one-pixel border so edges stay crisp.

BG = "#0D0F13"
"""Window background."""
SIDEBAR = "#111318"
SURFACE = "#15181E"
"""Panels and cards."""
SURFACE_2 = "#1B1F27"
"""Rows and controls sitting on a panel."""
SURFACE_3 = "#232833"
"""Hover state for anything on SURFACE_2."""
INSET = "#0A0C0F"
"""Recessed areas: the log, the code editor, read-only output."""
BORDER = "#242933"
BORDER_STRONG = "#313846"

TEXT = "#E7EAF0"
TEXT_2 = "#A2AABA"
TEXT_3 = "#6C7486"
TEXT_DISABLED = "#4A5060"

ACCENT = "#5B7CFF"
ACCENT_HOVER = "#4A6AF0"
ACCENT_TEXT = "#8FA6FF"
"""Accent for text on dark surfaces, where the button blue is too dim."""

SUCCESS = "#3DD68C"
WARNING = "#F5B946"
ORANGE = "#F59746"
DANGER = "#F25C5F"
DANGER_SOLID = "#D9454B"
DANGER_SOLID_HOVER = "#C23A40"
INFO = "#7CB4FF"



def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def tint(color: str, amount: float = 0.16, base: str = SURFACE) -> str:
    """``color`` laid over ``base`` at ``amount`` opacity, as an opaque hex.

    Tk has no alpha, so tinted backgrounds — the soft fill behind a status pill —
    are pre-blended against the surface they will sit on.
    """
    fg, bg = _rgb(color), _rgb(base)
    mixed = (round(b + (f - b) * amount) for f, b in zip(fg, bg))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def lighten(color: str, amount: float = 0.35) -> str:
    """Mix ``color`` toward white — makes a dark brand colour legible as text."""
    return tint("#FFFFFF", amount, color)


#: Severity accents. Both spellings of each level are present because the log
#: uses "warn"/"success" while :mod:`core.compose` uses "warning"/"info".
LEVEL_ACCENTS = {
    "error": DANGER,
    "warning": WARNING,
    "warn": WARNING,
    "info": INFO,
    "success": SUCCESS,
    "saved": SUCCESS,
}

#: Container and stack states.
STATE_COLORS = {
    "running": SUCCESS,
    "healthy": SUCCESS,
    "partial": ORANGE,
    "starting": WARNING,
    "restarting": WARNING,
    "unhealthy": DANGER,
    "paused": TEXT_2,
    "stopped": TEXT_3,
    "exited": TEXT_3,
    "created": TEXT_2,
    "unknown": TEXT_2,
}


#: Identity colours for telling hosts apart while several report at once. Spread
#: around the hue wheel at similar lightness, and steering clear of the pure
#: green, amber and red that already mean success, warning and error.
HOST_COLORS = (
    "#6FA8FF",  # blue
    "#E58CFF",  # orchid
    "#4FD6D0",  # turquoise
    "#FF9EC4",  # pink
    "#B7A1FF",  # violet
    "#C4DE6A",  # lime
    "#FFAE80",  # peach
    "#5CC8FF",  # sky
    "#D9C48A",  # sand
    "#F08CB0",  # rose
    "#8FD4A8",  # sage
    "#A9B8D6",  # steel
)


def host_color(index: int) -> str:
    """The identity colour for the ``index``-th host, cycling past the palette's end."""
    return HOST_COLORS[index % len(HOST_COLORS)]


def state_color(state: str) -> str:
    """Colour for a container or stack state, falling back to neutral grey."""
    return STATE_COLORS.get((state or "").lower(), STATE_COLORS["unknown"])


# ──────────────────────────────────────────────────────────────────────────────
# Typography
# ──────────────────────────────────────────────────────────────────────────────

# Plain Segoe UI ahead of its variable sibling: Tk draws through GDI, which is
# long proven with the former and inconsistent with variable-font instances.
_UI_CANDIDATES = (
    "Segoe UI", "Inter", "SF Pro Text",
    "Helvetica Neue", "Cantarell", "DejaVu Sans",
)
_SEMIBOLD_CANDIDATES = ("Segoe UI Semibold", "Inter SemiBold")
_MONO_CANDIDATES = (
    "Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas",
    "SF Mono", "Menlo", "DejaVu Sans Mono",
)

# Windows' own icon fonts: Fluent on 11, MDL2 on 10. Both use the same codepoints.
_ICON_CANDIDATES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

UI_FAMILY = "Segoe UI"
ICON_FAMILY: Optional[str] = None
"""An icon font when the system has one; otherwise icons fall back to text glyphs."""
SEMIBOLD_FAMILY: Optional[str] = None
"""A true semibold face when installed; otherwise strong text uses bold."""
MONO_FAMILY = "Consolas"

#: role -> (size, strong, mono)
_ROLES = {
    "display": (20, True, False),
    "title": (15, True, False),
    "heading": (13, True, False),
    "body": (13, False, False),
    "body_strong": (13, True, False),
    "small": (12, False, False),
    "small_strong": (12, True, False),
    "caption": (11, False, False),
    "overline": (10, True, False),
    "mono": (12, False, True),
    "mono_small": (11, False, True),
}
_font_cache: dict[str, ctk.CTkFont] = {}


def init_fonts(root: tk.Misc) -> None:
    """Pick the best installed families. Call once, after the root exists."""
    global UI_FAMILY, SEMIBOLD_FAMILY, MONO_FAMILY, ICON_FAMILY
    installed = set(tkfont.families(root))
    ICON_FAMILY = next((f for f in _ICON_CANDIDATES if f in installed), None)
    UI_FAMILY = next((f for f in _UI_CANDIDATES if f in installed), UI_FAMILY)
    SEMIBOLD_FAMILY = next((f for f in _SEMIBOLD_CANDIDATES if f in installed), None)
    MONO_FAMILY = next((f for f in _MONO_CANDIDATES if f in installed), MONO_FAMILY)
    ctk.ThemeManager.theme["CTkFont"].update({"family": UI_FAMILY, "size": 13})
    _font_cache.clear()
    _tk_font_cache.clear()


def font(role: str = "body") -> ctk.CTkFont:
    """A shared font object for a type role (see :data:`_ROLES`)."""
    cached = _font_cache.get(role)
    if cached is not None:
        return cached
    size, strong, mono = _ROLES[role]
    if mono:
        made = ctk.CTkFont(family=MONO_FAMILY, size=size)
    elif strong and SEMIBOLD_FAMILY:
        made = ctk.CTkFont(family=SEMIBOLD_FAMILY, size=size)
    else:
        made = ctk.CTkFont(family=UI_FAMILY, size=size, weight="bold" if strong else "normal")
    _font_cache[role] = made
    return made


_tk_font_cache: dict[tuple, tkfont.Font] = {}


def tk_font(widget: tk.Misc, role: str = "body") -> tkfont.Font:
    """A type role as a plain Tk font, pre-scaled, for text drawn on a canvas.

    Canvas text does not pass through CustomTkinter, so it gets neither the
    CTkFont nor the high-DPI factor; this applies both, and caches per scale.
    """
    scale = widget_scale(widget)
    key = (role, scale)
    cached = _tk_font_cache.get(key)
    if cached is not None:
        return cached
    size, strong, mono = _ROLES[role]
    pixels = -round(size * scale)
    if mono:
        made = tkfont.Font(root=widget, family=MONO_FAMILY, size=pixels)
    elif strong and SEMIBOLD_FAMILY:
        made = tkfont.Font(root=widget, family=SEMIBOLD_FAMILY, size=pixels)
    else:
        made = tkfont.Font(root=widget, family=UI_FAMILY, size=pixels,
                           weight="bold" if strong else "normal")
    _tk_font_cache[key] = made
    return made


#: name -> (codepoint in the system icon font, glyph to use without one)
_ICONS = {
    "edit": ("", "✎"),
    "close": ("", "✕"),
}


def icon(name: str) -> str:
    """The character that draws icon ``name`` in :func:`icon_font`."""
    codepoint, fallback = _ICONS[name]
    return codepoint if ICON_FAMILY else fallback


def icon_font(widget: tk.Misc, size: int = 12) -> tkfont.Font:
    """The font to draw :func:`icon` characters with, pre-scaled for a canvas."""
    if not ICON_FAMILY:
        return tk_font(widget, "body")
    scale = widget_scale(widget)
    key = ("icon", size, scale)
    cached = _tk_font_cache.get(key)
    if cached is None:
        cached = _tk_font_cache[key] = tkfont.Font(
            root=widget, family=ICON_FAMILY, size=-round(size * scale))
    return cached


def line_height(role: str) -> int:
    """A label height that hugs one line of ``role`` text.

    CTkLabel defaults to 28 px whatever the font, which is what spreads stacked
    labels apart and lets them overflow the card they sit in.
    """
    return round(_ROLES[role][0] * 1.45) + 2


def mono_tuple(size: int = 12) -> tuple[str, int]:
    """Font tuple for raw Tk widgets, which do not accept a CTkFont.

    Negative means pixels — the unit CTkFont uses — so monospace text lines up
    with the UI type instead of growing with the display's DPI setting twice.
    """
    return (MONO_FAMILY, -size)


def scaled_mono(widget: tk.Misc, size: int = 12) -> tuple[str, int]:
    """:func:`mono_tuple` pre-scaled for a raw Tk widget.

    CustomTkinter widgets scale their own fonts for high-DPI displays; plain Tk
    widgets (the code editor, tag fonts) do not, so they need the factor applied.
    """
    return (MONO_FAMILY, -round(size * widget_scale(widget)))


def widget_scale(widget: tk.Misc) -> float:
    try:
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except (AttributeError, KeyError, tk.TclError):
        return 1.0


# ──────────────────────────────────────────────────────────────────────────────
# Global CustomTkinter theme
# ──────────────────────────────────────────────────────────────────────────────

def apply() -> None:
    """Install the palette as CustomTkinter's theme. Call before the root window."""
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    t = ctk.ThemeManager.theme

    def put(widget: str, **values) -> None:
        for key, value in values.items():
            # Every colour is a [light, dark] pair; the app is dark-only.
            t[widget][key] = [value, value] if isinstance(value, str) and value.startswith("#") else value

    put("CTk", fg_color=BG)
    put("CTkToplevel", fg_color=BG)
    put("CTkFrame", corner_radius=10, border_width=0, fg_color=SURFACE,
        top_fg_color=SURFACE_2, border_color=BORDER)
    put("CTkButton", corner_radius=8, border_width=0, fg_color=ACCENT,
        hover_color=ACCENT_HOVER, border_color=BORDER_STRONG, text_color="#FFFFFF",
        text_color_disabled=TEXT_DISABLED)
    put("CTkLabel", text_color=TEXT)
    put("CTkEntry", corner_radius=8, border_width=1, fg_color=INSET,
        border_color=BORDER_STRONG, text_color=TEXT, placeholder_text_color=TEXT_3)
    put("CTkCheckBox", corner_radius=5, border_width=2, fg_color=ACCENT,
        border_color=BORDER_STRONG, hover_color=ACCENT_HOVER, checkmark_color="#FFFFFF",
        text_color=TEXT, text_color_disabled=TEXT_DISABLED)
    put("CTkSwitch", fg_color=SURFACE_3, progress_color=ACCENT, button_color=TEXT,
        button_hover_color="#FFFFFF", text_color=TEXT, text_color_disabled=TEXT_DISABLED)
    put("CTkProgressBar", corner_radius=2, border_width=0, fg_color=SURFACE_2,
        progress_color=ACCENT, border_color=BORDER)
    put("CTkOptionMenu", corner_radius=8, fg_color=SURFACE_2, button_color=SURFACE_3,
        button_hover_color=BORDER_STRONG, text_color=TEXT, text_color_disabled=TEXT_DISABLED)
    put("CTkComboBox", corner_radius=8, border_width=1, fg_color=INSET,
        border_color=BORDER_STRONG, button_color=SURFACE_3, button_hover_color=BORDER_STRONG,
        text_color=TEXT, text_color_disabled=TEXT_DISABLED)
    put("CTkScrollbar", corner_radius=6, border_spacing=3, fg_color="transparent",
        button_color=SURFACE_3, button_hover_color=BORDER_STRONG)
    put("CTkSegmentedButton", corner_radius=8, border_width=2, fg_color=SURFACE_2,
        selected_color=SURFACE_3, selected_hover_color=BORDER_STRONG,
        unselected_color=SURFACE_2, unselected_hover_color=SURFACE_3,
        text_color=TEXT, text_color_disabled=TEXT_DISABLED)
    put("CTkTextbox", corner_radius=8, border_width=0, fg_color=INSET,
        border_color=BORDER, text_color=TEXT, scrollbar_button_color=SURFACE_3,
        scrollbar_button_hover_color=BORDER_STRONG)
    put("CTkScrollableFrame", label_fg_color=SURFACE_2)
    put("DropdownMenu", fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT)


# ──────────────────────────────────────────────────────────────────────────────
# Widget recipes
# ──────────────────────────────────────────────────────────────────────────────

#: variant -> (fg, hover, text, border colour or "")
_BUTTON_VARIANTS = {
    "primary": (ACCENT, ACCENT_HOVER, "#FFFFFF", ""),
    "secondary": (SURFACE_2, SURFACE_3, TEXT, BORDER_STRONG),
    "ghost": ("transparent", SURFACE_3, TEXT_2, ""),
    "danger": (DANGER_SOLID, DANGER_SOLID_HOVER, "#FFFFFF", ""),
    "danger_soft": (tint(DANGER, 0.12, SURFACE_2), tint(DANGER, 0.22, SURFACE_2), DANGER, tint(DANGER, 0.35, SURFACE_2)),
    "warning_soft": (tint(WARNING, 0.10, SURFACE_2), tint(WARNING, 0.20, SURFACE_2), WARNING, tint(WARNING, 0.30, SURFACE_2)),
}
#: size -> (height, font role)
_BUTTON_SIZES = {"sm": (28, "small_strong"), "md": (34, "body_strong"), "lg": (40, "body_strong")}


class Button(ctk.CTkButton):
    """CTkButton that also looks disabled.

    CustomTkinter dims only the label of a disabled button, so a primary button
    keeps its full accent fill and reads as clickable. This swaps in a muted
    fill while disabled and restores the variant's colours afterwards.
    """

    def __init__(self, *args, **kwargs):
        state = kwargs.pop("state", "normal")
        super().__init__(*args, **kwargs)
        self._enabled_colors = self._color_options(kwargs)
        if state == "disabled":
            self.configure(state="disabled")

    @staticmethod
    def _color_options(options: dict) -> dict:
        return {k: options[k] for k in ("fg_color", "border_color", "border_width")
                if k in options}

    def configure(self, require_redraw=False, **kwargs):
        colors = self._color_options(kwargs)
        if colors:
            self._enabled_colors.update(colors)
        state = kwargs.get("state")
        if state is None and colors and str(self.cget("state")) == "disabled":
            # Colours changed while disabled: remember them, keep looking disabled.
            for key in colors:
                kwargs.pop(key)
        elif state == "disabled":
            filled = self._enabled_colors.get("fg_color") not in (None, "transparent")
            kwargs.setdefault("fg_color", SURFACE_2 if filled else "transparent")
            if filled:
                kwargs.setdefault("border_color", BORDER)
                kwargs.setdefault("border_width", 1)
        elif state == "normal":
            for key, value in self._enabled_colors.items():
                kwargs.setdefault(key, value)
        return super().configure(require_redraw=require_redraw, **kwargs)

    config = configure


def button(
    parent,
    text: str,
    command: Optional[Callable] = None,
    *,
    variant: str = "secondary",
    size: str = "md",
    width: int = 0,
    **kwargs,
) -> Button:
    """A button in one of the system's variants: primary, secondary, ghost, danger…

    ``width=0`` sizes the button to its label, which is what keeps a toolbar of
    mixed labels evenly padded instead of a row of arbitrary fixed widths.
    """
    fg, hover, text_color, border = _BUTTON_VARIANTS[variant]
    height, role = _BUTTON_SIZES[size]
    face = font(role)
    if not width:
        # Fit the label with even padding. CTkButton grows for longer text but
        # never shrinks, so a button whose label changes should pass a width.
        width = max(face.measure(text) + (28 if size == "sm" else 36), height)
    options = dict(
        text=text,
        command=command,
        height=height,
        width=width,
        corner_radius=8,
        fg_color=fg,
        hover_color=hover,
        text_color=text_color,
        text_color_disabled=TEXT_DISABLED,
        border_width=1 if border else 0,
        border_color=border or BORDER,
        font=face,
    )
    options.update(kwargs)
    return Button(parent, **options)


def variant_colors(variant: str) -> dict:
    """The configure() options that switch an existing button to ``variant``."""
    fg, hover, text_color, border = _BUTTON_VARIANTS[variant]
    return dict(fg_color=fg, hover_color=hover, text_color=text_color,
                border_width=1 if border else 0, border_color=border or BORDER)


def icon_button(parent, glyph: str, command=None, *, color: str = TEXT_2,
                size: int = 28, hover: str = SURFACE_3, **kwargs) -> ctk.CTkButton:
    """A square ghost button holding a single glyph.

    ``glyph`` may be the name of an :func:`icon`, drawn in the system icon font.
    """
    face = ctk.CTkFont(family=UI_FAMILY, size=13)
    if glyph in _ICONS:
        if ICON_FAMILY:
            face = ctk.CTkFont(family=ICON_FAMILY, size=12)
        glyph = icon(glyph)
    options = dict(
        text=glyph, command=command, width=size, height=size, corner_radius=7,
        fg_color="transparent", hover_color=hover, text_color=color,
        font=face,
    )
    options.update(kwargs)
    return ctk.CTkButton(parent, **options)


def card(parent, *, fg_color: str = SURFACE, border: bool = True, radius: int = 12,
         **kwargs) -> ctk.CTkFrame:
    """A bordered panel — the basic unit of layout."""
    return ctk.CTkFrame(
        parent, fg_color=fg_color, corner_radius=radius,
        border_width=1 if border else 0, border_color=BORDER, **kwargs,
    )


def text(parent, value: str = "", role: str = "body", color: str = TEXT, **kwargs) -> ctk.CTkLabel:
    """A plain label in a type role, sized to its line."""
    kwargs.setdefault("height", line_height(role))
    kwargs.setdefault("anchor", "w")
    return ctk.CTkLabel(parent, text=value, font=font(role), text_color=color, **kwargs)


def fit_text(text: str, limit: int, measure: Callable[[str], int]) -> str:
    """``text`` if ``measure`` puts it within ``limit``; otherwise its longest
    prefix that fits with an ellipsis on the end."""
    if limit <= 0 or not text or measure(text) <= limit:
        return text
    low, high = 0, len(text) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle].rstrip() + "…") <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def elide(label: ctk.CTkLabel, value: str, role: str) -> None:
    """Keep ``label`` showing ``value``, cut with an ellipsis to the width it gets.

    A CTkLabel that is given less room than its text needs simply loses the end
    of it at the edge. The label must be laid out to fill its cell (``fill="x"``
    or ``sticky="ew"``) so that its width is the room available, not its text's.
    """
    shown = [value]

    def fit(_event=None) -> None:
        try:
            width = label.winfo_width()
            if width <= 1:
                return
            text = fit_text(value, width - 2, tk_font(label, role).measure)
            if text != shown[0]:
                shown[0] = text
                label.configure(text=text)
        except tk.TclError:
            pass

    tk.Misc.bind(label, "<Configure>", fit, "+")


def pill(parent, text: str = "", color: str = TEXT_2, *, base: str = SURFACE_2,
         dot: bool = True, **kwargs) -> ctk.CTkLabel:
    """A rounded status chip: tinted fill, coloured text, optional leading dot."""
    label = ctk.CTkLabel(
        parent, text="", height=22, corner_radius=11, font=font("small_strong"), **kwargs
    )
    set_pill(label, text, color, base=base, dot=dot)
    return label


def set_pill(label: ctk.CTkLabel, text: str, color: str, *, base: str = SURFACE_2,
             dot: bool = True) -> None:
    label.configure(
        text=f"  {'●  ' if dot else ''}{text}  " if text else "",
        text_color=color,
        fg_color=tint(color, 0.14, base) if text else "transparent",
    )


def divider(parent, *, vertical: bool = False, color: str = BORDER) -> tk.Frame:
    """A one-pixel rule. Plain Tk: a 1 px CTkFrame rounds away to nothing."""
    if vertical:
        return tk.Frame(parent, width=1, height=20, bg=color, bd=0, highlightthickness=0)
    return tk.Frame(parent, height=1, bg=color, bd=0, highlightthickness=0)


def overline(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """A small uppercase section label."""
    kwargs.setdefault("height", line_height("overline"))
    kwargs.setdefault("text_color", TEXT_3)
    kwargs.setdefault("anchor", "w")
    return ctk.CTkLabel(parent, text=text.upper(), font=font("overline"), **kwargs)


def heading(parent, text: str, role: str = "title", **kwargs) -> ctk.CTkLabel:
    kwargs.setdefault("height", line_height(role))
    return ctk.CTkLabel(parent, text=text, font=font(role), text_color=TEXT, anchor="w", **kwargs)


def caption(parent, text: str = "", color: str = TEXT_3, role: str = "caption", **kwargs) -> ctk.CTkLabel:
    kwargs.setdefault("height", line_height(role))
    kwargs.setdefault("anchor", "w")
    kwargs.setdefault("justify", "left")
    return ctk.CTkLabel(parent, text=text, font=font(role), text_color=color, **kwargs)


def scrollable(parent, **kwargs) -> ctk.CTkScrollableFrame:
    """A transparent scroll area whose scrollbar only shows its thumb."""
    options = dict(label_text="", fg_color="transparent", corner_radius=0,
                   scrollbar_button_color=SURFACE_3,
                   scrollbar_button_hover_color=BORDER_STRONG)
    options.update(kwargs)
    frame = SmoothScrollableFrame(parent, **options)
    autohide_scrollbar(frame)
    return frame


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """CTkScrollableFrame whose mouse wheel glides instead of jumping.

    CustomTkinter moves the content 20 px per wheel notch in a single step. The
    content is a tree of native child windows, which Windows repaints one at a
    time, so a jump that size shows as tearing. Spreading each notch over a few
    frames keeps every individual move small enough to read as motion.
    """

    _NOTCH_PX = 64
    _TAU = 0.055
    """Seconds for the remaining distance to fall to about a third."""

    def _mouse_wheel_all(self, event):
        if (not sys.platform.startswith("win") or self._orientation != "vertical"
                or getattr(self, "_shift_pressed", False)):
            return super()._mouse_wheel_all(event)
        if not self._check_if_valid_scroll(event.widget):
            return None
        if self._parent_canvas.yview() == (0.0, 1.0):
            return None
        pending = self.__dict__.get("_dsm_pending", 0.0)
        step = -event.delta / 120 * self._NOTCH_PX * widget_scale(self)
        if pending * step < 0:
            pending = 0.0  # Reversed direction: drop what was left of the old glide.
        self._dsm_pending = pending + step
        if self.__dict__.get("_dsm_glide") is None:
            self._dsm_clock = time.perf_counter()
            self._glide()
        return None

    def _glide(self) -> None:
        self._dsm_glide = None
        now = time.perf_counter()
        elapsed, self._dsm_clock = max(now - self._dsm_clock, 0.008), now
        pending = self._dsm_pending
        if abs(pending) < 1:
            self._dsm_pending = 0.0
            return
        move = pending * (1 - math.exp(-elapsed / self._TAU))
        pixels = round(move) or (1 if pending > 0 else -1)
        try:
            canvas = self._parent_canvas
            canvas.yview_scroll(pixels, "units")
            first, last = canvas.yview()
            if (pixels < 0 and first <= 0.0) or (pixels > 0 and last >= 1.0):
                self._dsm_pending = 0.0
                return
            self._dsm_pending = pending - pixels
            self._dsm_glide = self.after(8, self._glide)
        except tk.TclError:
            self._dsm_pending = 0.0


def autohide_scrollbar(frame: ctk.CTkScrollableFrame) -> None:
    """Show a scrollable frame's scrollbar only while there is something to scroll.

    CustomTkinter always draws it, and with short content the thumb fills the
    whole track — a grey bar that reads as decoration rather than a control.
    """
    pending = {"job": None}

    def sync() -> None:
        pending["job"] = None
        try:
            canvas, bar = frame._parent_canvas, frame._scrollbar
            needed = frame.winfo_reqheight() > canvas.winfo_height() + 2
            if needed and not bar.winfo_ismapped():
                bar.grid()
            elif not needed and bar.winfo_ismapped():
                bar.grid_remove()
                canvas.yview_moveto(0)
        except (AttributeError, tk.TclError):
            pass

    def schedule(_event=None) -> None:
        if pending["job"] is None:
            try:
                pending["job"] = frame.after(30, sync)
            except tk.TclError:
                pass

    try:
        frame.bind("<Configure>", schedule, add="+")
        frame._parent_canvas.bind("<Configure>", schedule, add="+")
    except AttributeError:
        pass


def style_textbox(textbox: ctk.CTkTextbox) -> None:
    """Selection and cursor colours for a read-only output area."""
    inner = getattr(textbox, "_textbox", textbox)
    try:
        inner.configure(selectbackground=tint(ACCENT, 0.40, INSET), selectforeground=TEXT,
                        insertbackground=TEXT_2, spacing1=1, spacing3=1)
    except tk.TclError:
        pass


def bind_hover(widget: tk.Misc, on_enter: Callable[[], None], on_leave: Callable[[], None]) -> None:
    """Hover callbacks for a composite widget, stable as the pointer crosses children.

    Tk sends ``<Leave>`` to a frame the moment the pointer moves onto one of its
    own children, so a naive binding flickers. Leaving is confirmed by checking the
    pointer against the widget's rectangle after the event settles.
    """
    state = {"inside": False}

    def enter(_event=None):
        if not state["inside"]:
            state["inside"] = True
            on_enter()

    def check_leave():
        try:
            x, y = widget.winfo_pointerxy()
            left, top = widget.winfo_rootx(), widget.winfo_rooty()
            inside = left <= x < left + widget.winfo_width() and top <= y < top + widget.winfo_height()
        except tk.TclError:
            return
        if not inside and state["inside"]:
            state["inside"] = False
            on_leave()

    def leave(_event=None):
        try:
            widget.after(15, check_leave)
        except tk.TclError:
            pass

    def bind_all(target: tk.Misc) -> None:
        # Raw Tk binds: CTk's bind() forwards to a widget's internal canvas, which
        # this walk visits anyway, so using it would bind those parts twice.
        tk.Misc.bind(target, "<Enter>", enter, "+")
        tk.Misc.bind(target, "<Leave>", leave, "+")
        for child in target.winfo_children():
            bind_all(child)

    bind_all(widget)


def bind_click(widget: tk.Misc, callback: Callable, *, skip: tuple = ()) -> None:
    """Bind a left click across a composite widget, except on ``skip`` subtrees."""
    if widget in skip:
        return
    tk.Misc.bind(widget, "<Button-1>", lambda _e: callback(), "+")
    for child in widget.winfo_children():
        bind_click(child, callback, skip=skip)


class FloatingTip:
    """The tooltip bubble itself: shown at a point, after a delay.

    Separate from :class:`Tooltip` so that things which are not widgets — a
    region of a canvas — can have hints too.
    """

    DELAY_MS = 500

    def __init__(self, owner: tk.Misc):
        self._owner = owner
        self._job: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None

    def schedule(self, text: str, position: Callable[[], tuple[int, int]]) -> None:
        """Show ``text`` at ``position()`` (screen coordinates) after the delay."""
        self.hide()
        if not text:
            return
        try:
            self._job = self._owner.after(self.DELAY_MS, lambda: self._show(text, position))
        except tk.TclError:
            self._job = None

    def _show(self, text: str, position: Callable[[], tuple[int, int]]) -> None:
        self._job = None
        try:
            x, y = position()
            tip = tk.Toplevel(self._owner)
        except tk.TclError:
            return
        tip.wm_overrideredirect(True)
        tip.configure(bg=BORDER_STRONG)
        tk.Label(
            tip, text=text, bg=SURFACE_3, fg=TEXT, padx=9, pady=4,
            font=(UI_FAMILY, 9), justify="left",
        ).pack(padx=1, pady=1)
        tip.wm_geometry(f"+{x}+{y}")
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._tip = tip

    def hide(self) -> None:
        if self._job is not None:
            try:
                self._owner.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class Tooltip:
    """A small delayed hint beside a widget. Hover for half a second to show it."""

    def __init__(self, widget: tk.Misc, text: str):
        self._widget = widget
        self.text = text
        self._tip = FloatingTip(widget)
        # bind_hover, not <Enter>/<Leave>: a CTkButton is a canvas with a label
        # on top, and crossing between the two would hide and re-arm the tip.
        bind_hover(widget, self._schedule, self._hide)
        tk.Misc.bind(widget, "<ButtonPress>", self._hide, "+")
        for child in widget.winfo_children():
            tk.Misc.bind(child, "<ButtonPress>", self._hide, "+")

    def _schedule(self, _event=None) -> None:
        self._tip.schedule(self.text, self._below)

    def _below(self) -> tuple[int, int]:
        widget = self._widget
        return widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 6

    def _hide(self, _event=None) -> None:
        self._tip.hide()


# ──────────────────────────────────────────────────────────────────────────────
# Motion
# ──────────────────────────────────────────────────────────────────────────────
# Short and few: an eased colour change on hover, a fade as a window opens, a
# spinner while something runs. Each is a handful of after() steps, cancelled
# cleanly if the widget goes away mid-animation.

_FRAME_MS = 16


def blend(start: str, end: str, t: float) -> str:
    """The colour ``t`` of the way from ``start`` to ``end``."""
    return tint(end, max(0.0, min(1.0, t)), start)


def animate_color(widget, option: str, start: str, end: str, *, duration: int = 110,
                  on_frame: Optional[Callable[[str], None]] = None) -> None:
    """Ease ``widget``'s ``option`` from ``start`` to ``end``.

    A second call on the same widget and option cancels the first, so a quick
    pointer pass over a list does not leave rows half-animated.
    """
    jobs = widget.__dict__.setdefault("_dsm_anim", {})
    previous = jobs.pop(option, None)
    if previous is not None:
        try:
            widget.after_cancel(previous)
        except tk.TclError:
            pass
    if start == "transparent" or end == "transparent" or start == end:
        _set_color(widget, option, end, on_frame)
        return
    steps = max(1, duration // _FRAME_MS)

    def frame(step: int) -> None:
        t = step / steps
        eased = 1 - (1 - t) ** 3
        _set_color(widget, option, blend(start, end, eased) if step < steps else end, on_frame)
        if step < steps:
            try:
                jobs[option] = widget.after(_FRAME_MS, lambda: frame(step + 1))
            except tk.TclError:
                pass
        else:
            jobs.pop(option, None)

    frame(1)


def ease_fill(widget, target: str, *, duration: int = 110) -> None:
    """Ease a frame's fill to ``target`` from whatever it shows right now.

    Tracks the colour actually on screen, so leaving a row halfway through its
    hover animation reverses from the midpoint instead of jumping.
    """
    def frame_color(color: str) -> None:
        widget.__dict__["_dsm_fill"] = color
        widget.configure(fg_color=color)

    current = widget.__dict__.get("_dsm_fill") or str(widget.cget("fg_color"))
    if current == target:
        return
    animate_color(widget, "fg_color", current, target, duration=duration, on_frame=frame_color)


def hover_row(row, *, normal: str = SURFACE_2, hover: str = SURFACE_3,
              border: str = BORDER, border_hover: str = BORDER_STRONG) -> None:
    """Give a bordered row an eased hover state."""
    def paint(active: bool) -> None:
        ease_fill(row, hover if active else normal)
        try:
            row.configure(border_color=border_hover if active else border)
        except tk.TclError:
            pass

    bind_hover(row, lambda: paint(True), lambda: paint(False))


def _set_color(widget, option: str, color: str, on_frame) -> None:
    try:
        if on_frame is not None:
            on_frame(color)
        else:
            widget.configure(**{option: color})
    except tk.TclError:
        pass


def fade_in(window: tk.Misc, duration: int = 140) -> None:
    """Fade a new top-level in. A no-op where the platform has no window alpha."""
    try:
        window.attributes("-alpha", 0.0)
    except tk.TclError:
        return
    steps = max(1, duration // _FRAME_MS)

    def frame(step: int) -> None:
        try:
            window.attributes("-alpha", min(1.0, step / steps))
            if step < steps:
                window.after(_FRAME_MS, lambda: frame(step + 1))
        except tk.TclError:
            pass

    window.after(30, lambda: frame(1))
    # Belt and braces: never leave a window invisible if a frame is dropped.
    window.after(duration + 400, lambda: _opaque(window))


def _opaque(window: tk.Misc) -> None:
    try:
        window.attributes("-alpha", 1.0)
    except tk.TclError:
        pass


def search_icon(parent, *, size: int = 14, color: str = TEXT_3, bg: str = INSET) -> tk.Canvas:
    """A drawn magnifying glass. Font glyphs for it are missing or tiny in most UI faces."""
    scale = widget_scale(parent)
    px = round(size * scale)
    canvas = tk.Canvas(parent, width=px, height=px, bg=bg, highlightthickness=0, bd=0)
    stroke = max(1.5, 1.6 * scale)
    radius = px * 0.32
    cx = cy = px * 0.42
    canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                       outline=color, width=stroke)
    offset = radius * 0.72
    canvas.create_line(cx + offset, cy + offset, px - stroke, px - stroke,
                       fill=color, width=stroke + 0.5, capstyle="round")
    return canvas


class Spinner(tk.Canvas):
    """A small rotating arc, for "working" without a percentage."""

    def __init__(self, parent, size: int = 14, color: str = ACCENT, bg: str = BG,
                 width: int = 2):
        scale = widget_scale(parent)
        px = round(size * scale)
        super().__init__(parent, width=px, height=px, bg=bg, highlightthickness=0, bd=0)
        pad = max(2, round(width * scale))
        self.create_oval(pad, pad, px - pad, px - pad, outline=tint(color, 0.22, bg),
                         width=round(width * scale))
        self._arc = self.create_arc(pad, pad, px - pad, px - pad, start=90, extent=100,
                                    style="arc", outline=color, width=round(width * scale))
        self._angle = 90
        self._job: Optional[str] = None

    def start(self) -> None:
        if self._job is None:
            self._spin()

    def stop(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _spin(self) -> None:
        self._angle = (self._angle - 18) % 360
        try:
            self.itemconfigure(self._arc, start=self._angle)
            self._job = self.after(30, self._spin)
        except tk.TclError:
            self._job = None

    def destroy(self) -> None:
        self.stop()
        super().destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Drawn shapes
# ──────────────────────────────────────────────────────────────────────────────
# Tk's canvas draws ovals and arcs without anti-aliasing, and CustomTkinter's
# smooth corners only exist inside its own widgets. Anything drawn directly on a
# canvas — the host list — gets its curves from small images instead: the pixels
# are computed here with proper edge coverage and handed to Tk as PNG, alpha
# included. No imaging library is involved.

def _png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA pixels as a PNG file."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    stride = width * 4
    rows = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def _coverage(x: float, y: float, width: float, height: float, radius: float) -> float:
    """How much of pixel ``(x, y)`` a ``width``×``height`` rounded rectangle covers.

    The signed distance from the pixel centre to the shape's edge, clamped to one
    pixel, is the anti-aliasing: 1 inside, 0 outside, a ramp across the boundary.
    """
    if width <= 0 or height <= 0:
        return 0.0
    px, py = x + 0.5 - width / 2, y + 0.5 - height / 2
    qx, qy = abs(px) - (width / 2 - radius), abs(py) - (height / 2 - radius)
    distance = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
    return max(0.0, min(1.0, 0.5 - distance))


def shape_pixels(width: int, height: int, radius: float, color: str, *,
                 alpha: float = 1.0, outline: int = 0) -> bytes:
    """RGBA pixels of a rounded rectangle; with ``outline``, only a ring that wide."""
    radius = max(0.0, min(radius, width / 2, height / 2))
    rgb = bytes(_rgb(color))

    def value(x: int, y: int) -> int:
        cover = _coverage(x, y, width, height, radius)
        if outline:
            cover -= _coverage(x - outline, y - outline, width - 2 * outline,
                               height - 2 * outline, max(radius - outline, 0.0))
        return max(0, min(255, round(cover * alpha * 255)))

    # A rounded rectangle is mirror-symmetric and constant between its corners,
    # so only the leading edge of each row needs working out.
    edge = min((width + 1) // 2, math.ceil(radius) + outline + 1)
    out = bytearray()
    for y in range(height):
        left = [value(x, y) for x in range(edge)]
        middle = width - 2 * edge
        if middle >= 0:
            row = left + [value(edge, y)] * middle + left[::-1]
        else:  # Odd width, all edge: the centre pixel is shared by both halves.
            row = left + left[-2::-1]
        for a in row:
            out += rgb
            out.append(a)
    return bytes(out)


def _photo(master: tk.Misc, key: tuple, width: int, height: int,
           pixels: Callable[[], bytes]) -> tk.PhotoImage:
    """A cached PhotoImage. The cache lives on the root, which also keeps the
    images referenced — Tk drops an image the moment Python forgets it."""
    root = master._root()
    cache = root.__dict__.setdefault("_dsm_shapes", {})
    image = cache.get(key)
    if image is None:
        data = base64.b64encode(_png(width, height, pixels()))
        image = cache[key] = tk.PhotoImage(master=root, data=data, format="png")
    return image


def shape_image(master: tk.Misc, width: int, height: int, radius: float, color: str, *,
                alpha: float = 1.0, outline: int = 0) -> tk.PhotoImage:
    """An anti-aliased rounded rectangle (a circle, when the radius is half the
    size) for a canvas image item. ``alpha`` below 1 makes it a translucent wash."""
    width, height = max(1, int(width)), max(1, int(height))
    key = ("shape", width, height, round(radius, 2), color, round(alpha, 3), outline)
    return _photo(master, key, width, height,
                  lambda: shape_pixels(width, height, radius, color,
                                       alpha=alpha, outline=outline))


def fade_image(master: tk.Misc, width: int, height: int, color: str, *,
               downward: bool = True) -> tk.PhotoImage:
    """A strip of ``color`` fading to nothing — opaque at the top when
    ``downward``. Laid over the edge of a scrolling list, it lets rows dissolve
    into the edge instead of being cut off by it."""
    width, height = max(1, int(width)), max(1, int(height))

    def pixels() -> bytes:
        rgb = bytes(_rgb(color))
        out = bytearray()
        for y in range(height):
            t = 1.0 - (y + 0.5) / height
            if not downward:
                t = 1.0 - t
            # Squared: most of the strip stays faint, so text under it stays legible.
            out += (rgb + bytes((round(t * t * 255),))) * width
        return bytes(out)

    return _photo(master, ("fade", width, height, color, downward), width, height, pixels)


def corner_masks(master: tk.Misc, radius: int, color: str) -> dict[str, tk.PhotoImage]:
    """Four images that round the corners of a plain canvas rectangle.

    Each paints the area *outside* a quarter circle in ``color`` — the colour of
    whatever the rectangle sits on. Laid over a rectangle's corners they leave it
    looking rounded, and because they do not depend on the rectangle's own fill,
    that fill can animate by changing one canvas item.
    """
    radius = max(1, int(radius))

    def quadrant(flip_x: bool, flip_y: bool) -> bytes:
        rgb = bytes(_rgb(color))
        out = bytearray()
        for y in range(radius):
            for x in range(radius):
                cover = _coverage(radius - 1 - x if flip_x else x,
                                  radius - 1 - y if flip_y else y,
                                  2 * radius, 2 * radius, radius)
                out += rgb
                out.append(round((1.0 - cover) * 255))
        return bytes(out)

    return {
        anchor: _photo(master, ("corner", radius, color, anchor), radius, radius,
                       lambda fx=fx, fy=fy: quadrant(fx, fy))
        for anchor, fx, fy in (("nw", False, False), ("ne", True, False),
                               ("sw", False, True), ("se", True, True))
    }


# ──────────────────────────────────────────────────────────────────────────────
# Window chrome
# ──────────────────────────────────────────────────────────────────────────────

ASSETS = Path(__file__).resolve().parent / "assets"
ICON_ICO = ASSETS / "icon.ico"
ICON_PNG = ASSETS / "icon.png"
APP_ID = "DockerStackManager.Desktop"


def set_app_id() -> None:
    """Give the process its own taskbar identity, so it shows our icon, not Python's."""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass


def style_window(window: tk.Misc, *, fade: bool = True) -> None:
    """Icon, a title bar that matches the window, and a short fade-in.

    CustomTkinter's toplevels install their own icon ~200 ms after creation, so
    the chrome is applied once now and again after that has happened.
    """
    _apply_chrome(window)
    if fade:
        fade_in(window)
    try:
        window.after(260, lambda: _apply_chrome(window))
    except tk.TclError:
        pass


def _apply_chrome(window: tk.Misc) -> None:
    try:
        if not window.winfo_exists():
            return
    except tk.TclError:
        return
    _set_icon(window)
    if sys.platform.startswith("win"):
        _color_title_bar(window)


def _set_icon(window: tk.Misc) -> None:
    try:
        if sys.platform.startswith("win") and ICON_ICO.is_file():
            window.iconbitmap(str(ICON_ICO))
        elif ICON_PNG.is_file():
            image = tk.PhotoImage(file=str(ICON_PNG))
            window.iconphoto(False, image)
            window._dsm_icon = image  # Keep a reference; Tk does not.
    except (tk.TclError, OSError):
        pass


def _colorref(color: str) -> int:
    r, g, b = _rgb(color)
    return (b << 16) | (g << 8) | r


def _color_title_bar(window: tk.Misc) -> None:
    """Dark title bar in the window's own colour (Windows 11; dark-only on 10)."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attribute, value in (
            (20, 1),                    # DWMWA_USE_IMMERSIVE_DARK_MODE
            (35, _colorref(BG)),        # DWMWA_CAPTION_COLOR
            (34, _colorref(BORDER)),    # DWMWA_BORDER_COLOR
            (36, _colorref(TEXT_2)),    # DWMWA_TEXT_COLOR
        ):
            data = ctypes.c_int(value)
            set_attr(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    except (AttributeError, OSError, tk.TclError):
        pass


def round_corners(window: tk.Misc) -> None:
    """Ask Windows 11 to round a borderless window's corners and draw its border."""
    if not sys.platform.startswith("win"):
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attribute, value in ((33, 2), (34, _colorref(BORDER_STRONG))):
            data = ctypes.c_int(value)  # 33: corner preference (2 = round); 34: border
            set_attr(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    except (AttributeError, OSError, tk.TclError):
        pass


def present(window, parent, size: tuple[int, int], *,
            minsize: Optional[tuple[int, int]] = None,
            focus: Optional[Callable[[], None]] = None) -> None:
    """Size, centre, style and focus a new secondary window — the same five steps
    every window used to spell out for itself."""
    center_on(window, parent, *size)
    if minsize:
        window.minsize(*minsize)
    style_window(window)

    def bring_forward() -> None:
        try:
            window.lift()
            window.focus_force()
            if focus is not None:
                focus()
        except tk.TclError:
            pass

    window.after(80, bring_forward)


def center_on(window: tk.Misc, parent: tk.Misc, width: int, height: int) -> None:
    """Size ``window`` and centre it over ``parent``."""
    try:
        parent.update_idletasks()
        scale = window._get_window_scaling() if hasattr(window, "_get_window_scaling") else 1.0
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x = px + max(0, (pw - int(width * scale)) // 2)
        y = py + max(0, (ph - int(height * scale)) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")
    except tk.TclError:
        window.geometry(f"{width}x{height}")


def run_in_thread(task: Callable[[], None]) -> None:
    """Start ``task`` on a daemon thread. Shared so every window spells it the same."""
    import threading
    threading.Thread(target=task, daemon=True).start()
