"""Sidebar list of configured hosts, drawn as selectable cards on one canvas.

The list used to be a scrollable frame of widgets: every card a frame holding
labels, a switch and two buttons, each of those a native child window or three.
Scrolling moved a few hundred windows at once and Windows repainted them one
after another, which showed as tearing — worse whenever a hover animation was
recolouring a card as it slid under the pointer.

Here the whole list is canvas items instead. A canvas composes its items off
screen and puts the finished frame up in one copy, so scrolling, hover fades and
the switch animation cannot tear whatever the list is doing. The cost is that
hit-testing, hover and the scrollbar are done by hand below.
"""

import math
import time
import tkinter as tk
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Optional

from core import distro
from gui import theme
from models.host import UPDATES_FAILED, UPDATES_UNKNOWN, Host

HostAction = Callable[[Host], None]

_HOVER = theme.tint(theme.SURFACE_3, 0.6, theme.SIDEBAR)
_REMOVE_HOVER = theme.tint(theme.DANGER, 0.25, theme.SURFACE_2)

# Layout, in the same logical pixels CustomTkinter uses; scaled when drawn.
_GUTTER = 8
"""Strip on the trailing edge that the cards leave free for the scrollbar."""
_GAP = 4
_RADIUS = 10
_PAD = 9
_TEXT_X = 33
_DOT = 7
_HALO = 15
_SWITCH_W, _SWITCH_H, _KNOB = 28, 15, 11
_TOOL = 26
_THUMB = 4
_THUMB_MIN = 32
_FADE = 18
"""Depth of the soft edge where cards scroll out of view."""

_WHEEL_PX = 60
"""Distance one wheel notch travels."""
_SCROLL_TAU = 0.055
"""Seconds for the distance left in a scroll to fall to about a third."""
_FRAME_MS = 8
_SWITCH_STEPS = 6
"""Track colours pre-rendered between off and on for the switch's crossfade."""
_PULSE_MS = 1400
_PULSE_STEPS = 8
"""Halo strengths pre-rendered for the "working" pulse around a host's marker."""

_TIPS = {
    "switch": "Include in “Check for updates” and “Update all hosts”",
    "edit": "Edit host",
    "remove": "Remove host",
}


class _CardState:
    """What a card is showing mid-animation. Outlives redraws, so a refresh that
    lands halfway through a hover fade carries on from the colour on screen."""

    __slots__ = ("fill", "knob", "knob_target")

    def __init__(self, included: bool):
        self.fill = theme.SIDEBAR
        self.knob = self.knob_target = 1.0 if included else 0.0


@dataclass(slots=True)
class _Card:
    """One drawn card: where it is, and the canvas items that change later."""

    host: Host
    state: _CardState
    top: int
    bottom: int
    right: int
    name_limit: int
    name_limit_hovered: int
    background: int = 0
    name: int = 0
    track: int = 0
    knob: int = 0
    halo: int = 0
    """The pulsing ring behind the dot while the host is working, else 0."""
    track_x: int = 0
    tools: dict[str, tuple[int, int]] = field(default_factory=dict)
    """part -> (hover background item, glyph item)"""
    boxes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    """part -> its clickable rectangle, in canvas coordinates"""


class HostList(tk.Canvas):
    """Scrollable host cards.

    :meth:`set_hosts` replaces the list; :meth:`refresh` re-reads every host and
    redraws. A redraw is a few hundred canvas calls shown as one frame, so it is
    cheap enough to run once per host as a parallel update check reports in.
    """

    def __init__(
        self,
        parent,
        *,
        on_select: HostAction,
        on_edit: HostAction,
        on_remove: HostAction,
        on_toggle: Optional[HostAction] = None,
        on_activate: Optional[HostAction] = None,
    ):
        super().__init__(parent, bg=theme.SIDEBAR, highlightthickness=0, bd=0,
                         width=1, height=1, yscrollincrement=1, takefocus=0)
        self._on_select = on_select
        self._on_edit = on_edit
        self._on_remove = on_remove
        self._on_toggle = on_toggle
        self._on_activate = on_activate

        self._hosts: list[Host] = []
        self._active: Optional[Host] = None
        self._connected: Optional[Host] = None
        self._marks: dict[int, tuple[str, bool]] = {}
        """id(host) -> (identity colour, still working) during a bulk run."""
        self._pulse_job: Optional[str] = None
        self._states: dict[int, _CardState] = {}
        self._cards: list[_Card] = []
        self._tops: list[int] = []
        self._by_host: dict[int, _Card] = {}

        self._scale = 1.0
        self._drawn_for: tuple = ()
        self._content = 0
        self._offset = 0.0
        self._target = 0.0
        self._origin = 0
        self._scroll_job: Optional[str] = None
        self._scroll_clock = 0.0
        self._reveal_pending = False

        self._pointer: Optional[tuple[int, int]] = None
        self._hover: tuple[Optional[_Card], str] = (None, "")
        self._pressed: Optional[tuple[Host, str]] = None
        self._dragging = False
        self._drag_grab = 0.0

        self._thumb = 0
        self._fades: tuple[int, int] = (0, 0)
        self._thumb_color = theme.SIDEBAR
        self._thumb_job: Optional[str] = None

        self._animations: dict[tuple, tuple[float, float, Callable[[float], None]]] = {}
        self._animation_job: Optional[str] = None
        self._ellipsis_cache: dict[tuple, str] = {}
        self._menu: Optional[tk.Menu] = None
        self._tip = theme.FloatingTip(self)

        self.bind("<Configure>", self._on_configure)
        self.bind("<Motion>", self._on_motion)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<Double-ButtonPress-1>", lambda e: self._on_press(e, double=True))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-3>", self._on_menu)
        self.bind("<MouseWheel>", self._on_wheel)

    # ── Public API ──────────────────────────────────────────────────────────

    def set_hosts(self, hosts: list[Host]) -> None:
        """Replace the list. Call when a host is added, removed, or edited."""
        self._hosts = list(hosts)
        alive = {id(host) for host in self._hosts}
        self._states = {key: state for key, state in self._states.items() if key in alive}
        self._redraw()

    def set_active(self, host: Optional[Host]) -> None:
        """Highlight ``host`` as the selected one, scrolling it into view."""
        changed = host is not self._active
        self._active = host
        self._redraw()
        if changed:
            self._reveal_pending = True
            self._reveal_active()

    def set_connected(self, host: Optional[Host]) -> None:
        """Mark which host, if any, holds the live session."""
        self._connected = host
        self._redraw()

    def refresh(self) -> None:
        """Re-read every host and redraw its card."""
        self._redraw()

    def mark(self, host: Host, color: str, *, working: bool) -> None:
        """Show ``host`` in its identity ``color`` — the one its log lines carry.

        While ``working`` the marker pulses; afterwards it stays, steady, until
        :meth:`clear_marks`, so a finished host can still be matched to its output.
        """
        self._marks[id(host)] = (color, working)
        self._redraw()

    def clear_marks(self) -> None:
        """Drop every identity colour: the bulk run is over."""
        self._marks.clear()
        self._redraw()

    # ── Drawing ─────────────────────────────────────────────────────────────

    def _px(self, value: float) -> int:
        return round(value * self._scale)

    def _redraw(self) -> None:
        self._tip.hide()
        self.delete("all")
        self._cards, self._tops, self._by_host = [], [], {}
        self._hover = (None, "")
        self._thumb, self._fades = 0, (0, 0)
        width = self.winfo_width()
        if width <= 1:
            return  # Not laid out yet; <Configure> draws once there is a size.
        self._scale = theme.widget_scale(self)
        self._drawn_for = (width, self._scale)

        y = 0
        for host in self._hosts:
            card = self._draw_card(host, y, width)
            self._cards.append(card)
            self._tops.append(card.top)
            self._by_host[id(host)] = card
            y = card.bottom + self._px(_GAP)
        if not self._hosts:
            y = self._draw_empty_hint(width)
        self._content = y

        self._fades = (self.create_image(0, 0, anchor="nw", state="hidden"),
                       self.create_image(0, 0, anchor="sw", state="hidden"))
        self._thumb = self.create_line(
            0, 0, 0, 0, width=self._px(_THUMB), capstyle="round",
            fill=self._thumb_color, state="hidden",
        )
        self._fit_scroll()
        # Settle every card on its resting colour — the one that was selected a
        # moment ago fades out — then let the pointer claim the one it is over.
        for card in self._cards:
            self._paint_card(card, hovered=False)
        self._update_hover()
        self._pulse(reschedule=False)
        if self._pulse_job is None and any(card.halo for card in self._cards):
            self._pulse_job = self.after(_PULSE_MS // (2 * _PULSE_STEPS), self._pulse)

    def _draw_card(self, host: Host, top: int, width: int) -> _Card:
        px = self._px
        key = id(host)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _CardState(host.include_in_bulk)
        active = host is self._active
        connected = host is self._connected

        update_text, update_color = _update_summary(host)
        name_h = px(theme.line_height("body_strong"))
        row_h = px(theme.line_height("caption"))
        rows = 1 + bool(host.os_pretty) + bool(update_text)
        height = 2 * px(_PAD) + name_h + rows * row_h + (px(2) if update_text else 0)
        right = width - px(_GUTTER)
        bottom = top + height

        switch_x = right - px(10) - px(_SWITCH_W)
        tool = px(_TOOL)
        remove_x = switch_x - px(6) - tool
        edit_x = remove_x - px(2) - tool
        text_x = px(_TEXT_X)
        card = _Card(
            host=host, state=state, top=top, bottom=bottom, right=right,
            name_limit=switch_x - px(8) - text_x,
            name_limit_hovered=edit_x - px(6) - text_x,
            track_x=switch_x,
        )

        card.background = self.create_rectangle(0, top, right, bottom, width=0, fill=state.fill)
        radius = px(_RADIUS)
        masks = theme.corner_masks(self, radius, theme.SIDEBAR)
        for anchor, x, y in (("nw", 0, top), ("ne", right, top),
                             ("sw", 0, bottom), ("se", right, bottom)):
            self.create_image(x, y, image=masks[anchor], anchor=anchor)

        if active:
            # Selection marker: a slim accent bar on the leading edge.
            self.create_image(
                px(4), top + px(10), anchor="nw",
                image=theme.shape_image(self, px(3), height - 2 * px(10), px(1.5), theme.ACCENT),
            )

        name_y = top + px(_PAD) + name_h // 2
        dot = px(_DOT)
        dot_x = px(16) + dot // 2
        mark_color, working = self._marks.get(key, ("", False))
        if mark_color:
            # Mid bulk run the dot is the host's identity, matching its log lines.
            dot_color = mark_color
            if working:
                card.halo = self.create_image(dot_x, name_y)
        else:
            dot_color = theme.SUCCESS if connected else theme.BORDER_STRONG
            if connected:
                halo = px(_HALO)
                self.create_image(dot_x, name_y, image=theme.shape_image(
                    self, halo, halo, halo / 2, theme.SUCCESS, alpha=0.20))
        self.create_image(dot_x, name_y, image=theme.shape_image(
            self, dot, dot, dot / 2, dot_color))

        # Hosts left out of bulk actions recede, so the fleet you are acting on
        # reads at a glance.
        card.name = self.create_text(
            text_x, name_y, anchor="w", font=theme.tk_font(self, "body_strong"),
            fill=theme.TEXT if host.include_in_bulk else theme.TEXT_3,
            text=self._ellipsize(host.display_name, "body_strong", card.name_limit),
        )
        caption = theme.tk_font(self, "caption")
        limit = right - px(12) - text_x
        y = top + px(_PAD) + name_h + row_h // 2
        for text, color in ((host.address, theme.TEXT_3), (host.os_pretty, theme.TEXT_3)):
            if text:
                self.create_text(text_x, y, anchor="w", font=caption, fill=color,
                                 text=self._ellipsize(text, "caption", limit))
                y += row_h
        if update_text:
            self.create_text(text_x, y + px(2), anchor="w", font=caption, fill=update_color,
                             text=self._ellipsize(update_text, "caption", limit))

        # Trailing edge: the bulk-update switch, always visible, with edit and
        # remove appearing beside it on hover.
        switch_y = name_y - px(_SWITCH_H) // 2
        card.track = self.create_image(switch_x, switch_y, anchor="nw")
        card.knob = self.create_image(
            0, switch_y + (px(_SWITCH_H) - px(_KNOB)) // 2, anchor="nw",
            image=theme.shape_image(self, px(_KNOB), px(_KNOB), px(_KNOB) / 2, theme.TEXT),
        )
        slop = px(5)
        card.boxes["switch"] = (switch_x - slop, switch_y - slop,
                                switch_x + px(_SWITCH_W) + slop, switch_y + px(_SWITCH_H) + slop)
        target = 1.0 if host.include_in_bulk else 0.0
        if state.knob_target != target:
            self._slide_switch(host, state, target)
        self._place_switch(card)

        tool_y = name_y - tool // 2
        glyphs = theme.icon_font(self, 12)
        for part, x, glyph, hover in (("edit", edit_x, theme.icon("edit"), theme.SURFACE_3),
                                      ("remove", remove_x, theme.icon("close"), _REMOVE_HOVER)):
            backdrop = self.create_image(
                x, tool_y, anchor="nw", state="hidden",
                image=theme.shape_image(self, tool, tool, px(7), hover),
            )
            item = self.create_text(x + tool // 2, name_y, text=glyph, font=glyphs,
                                    fill=theme.TEXT_2, state="hidden")
            card.tools[part] = (backdrop, item)
            card.boxes[part] = (x, tool_y, x + tool, tool_y + tool)
        return card

    def _draw_empty_hint(self, width: int) -> int:
        """What the sidebar shows before any host exists. Returns its bottom edge."""
        px = self._px
        font = theme.tk_font(self, "small")
        lines = ("No hosts yet.", "Add one, or import the hosts", "from your SSH config.")
        line_h = px(theme.line_height("small"))
        top, right = px(8), width - px(_GUTTER)
        height = 2 * px(14) + line_h * len(lines)
        self.create_image(0, top, anchor="nw", image=theme.shape_image(
            self, right, height, px(_RADIUS), theme.BORDER, outline=max(1, px(1))))
        for index, line in enumerate(lines):
            self.create_text(px(14), top + px(14) + line_h * index + line_h // 2,
                             anchor="w", text=line, font=font, fill=theme.TEXT_3)
        return top + height

    def _ellipsize(self, text: str, role: str, limit: int) -> str:
        """``text`` cut to fit ``limit`` pixels, ending in an ellipsis if it had to be."""
        key = (text, role, limit, self._scale)
        cached = self._ellipsis_cache.get(key)
        if cached is not None:
            return cached
        fitted = theme.fit_text(text, limit, theme.tk_font(self, role).measure)
        if len(self._ellipsis_cache) > 2000:
            self._ellipsis_cache.clear()
        self._ellipsis_cache[key] = fitted
        return fitted

    def _pulse(self, reschedule: bool = True) -> None:
        """Breathe the halo of every host still working, all in step."""
        if reschedule:
            self._pulse_job = None
        working = [card for card in self._cards if card.halo]
        if not working:
            return
        phase = (time.perf_counter() * 1000 % _PULSE_MS) / _PULSE_MS
        level = round((0.5 - 0.5 * math.cos(phase * 2 * math.pi)) * _PULSE_STEPS)
        size = self._px(_HALO)
        try:
            for card in working:
                color = self._marks[id(card.host)][0]
                self.itemconfigure(card.halo, image=theme.shape_image(
                    self, size, size, size / 2, color,
                    alpha=0.10 + 0.32 * level / _PULSE_STEPS))
            if reschedule:
                self._pulse_job = self.after(_PULSE_MS // (2 * _PULSE_STEPS), self._pulse)
        except tk.TclError:
            pass

    def _place_switch(self, card: _Card) -> None:
        """Put the knob and track colour where ``card.state.knob`` (0–1) says."""
        px = self._px
        position = card.state.knob
        step = round(position * _SWITCH_STEPS)
        color = theme.blend(theme.SURFACE_3, theme.ACCENT, step / _SWITCH_STEPS)
        self.itemconfigure(card.track, image=theme.shape_image(
            self, px(_SWITCH_W), px(_SWITCH_H), px(_SWITCH_H) / 2, color))
        inset = (px(_SWITCH_H) - px(_KNOB)) // 2
        travel = px(_SWITCH_W) - px(_KNOB) - 2 * inset
        y = self.coords(card.knob)[1]
        self.coords(card.knob, card.track_x + inset + round(travel * position), y)

    def _slide_switch(self, host: Host, state: _CardState, target: float) -> None:
        start = state.knob
        state.knob_target = target

        def apply(progress: float) -> None:
            state.knob = start + (target - start) * progress
            card = self._by_host.get(id(host))
            if card is not None:
                self._place_switch(card)

        self._animate(("knob", id(host)), 140, apply)

    # ── Hover ───────────────────────────────────────────────────────────────

    def _locate(self, x: int, y: int) -> tuple[Optional[_Card], str]:
        """The card under a window point, and which part of it: ``body``,
        ``switch``, ``edit`` or ``remove`` — or ``thumb`` for the scrollbar strip."""
        if self._max_offset() > 0 and x >= self.winfo_width() - self._px(_GUTTER):
            return None, "thumb"
        cy = y + self._origin
        index = bisect_right(self._tops, cy) - 1
        if index < 0:
            return None, ""
        card = self._cards[index]
        if cy >= card.bottom or x >= card.right:
            return None, ""
        for part, (x0, y0, x1, y1) in card.boxes.items():
            if x0 <= x < x1 and y0 <= cy < y1:
                return card, part
        return card, "body"

    def _update_hover(self) -> None:
        """Bring hover state in line with where the pointer is over the content.

        Called on pointer motion and also after every scroll step and redraw:
        the content moves under a still pointer just as often as the reverse.
        """
        if self._dragging:
            card, part = None, "thumb"
        elif self._pointer is None:
            card, part = None, ""
        else:
            card, part = self._locate(*self._pointer)
        old_card, old_part = self._hover
        if card is old_card and part == old_part:
            return
        self._hover = (card, part)
        self._tip.hide()

        if old_card is not card:
            if old_card is not None:
                self._paint_card(old_card, hovered=False)
            if card is not None:
                self._paint_card(card, hovered=True)
        if old_card is not None and old_part in old_card.tools:
            self._paint_tool(old_card, old_part, hovered=False)
        if card is not None and part in card.tools:
            self._paint_tool(card, part, hovered=True)

        self.configure(cursor="hand2" if part in _TIPS else "")
        if card is not None and part in _TIPS:
            box = card.boxes[part]
            self._tip.schedule(_TIPS[part], lambda: (
                self.winfo_rootx() + box[0],
                self.winfo_rooty() + box[3] - self._origin + self._px(6),
            ))
        self._wake_thumb()

    def _paint_card(self, card: _Card, *, hovered: bool) -> None:
        host = card.host
        if host is self._active:
            target = theme.SURFACE_2
        else:
            target = _HOVER if hovered else theme.SIDEBAR
        self._ease_fill(host, target)
        for _backdrop, glyph in card.tools.values():
            self.itemconfigure(glyph, state="normal" if hovered else "hidden")
        limit = card.name_limit_hovered if hovered else card.name_limit
        self.itemconfigure(card.name, text=self._ellipsize(host.display_name, "body_strong", limit))

    def _paint_tool(self, card: _Card, part: str, *, hovered: bool) -> None:
        backdrop, glyph = card.tools[part]
        self.itemconfigure(backdrop, state="normal" if hovered else "hidden")
        lit = theme.DANGER if part == "remove" else theme.TEXT
        self.itemconfigure(glyph, fill=lit if hovered else theme.TEXT_2)

    def _ease_fill(self, host: Host, target: str) -> None:
        state = self._states.get(id(host))
        if state is None:
            return
        key = ("fill", id(host))
        self._animations.pop(key, None)
        start = state.fill
        if start == target:
            return

        def apply(progress: float) -> None:
            state.fill = theme.blend(start, target, progress)
            card = self._by_host.get(id(host))
            if card is not None:
                self.itemconfigure(card.background, fill=state.fill)

        self._animate(key, 100, apply)

    # ── Pointer events ──────────────────────────────────────────────────────

    def _on_motion(self, event) -> None:
        self._pointer = (event.x, event.y)
        if self._dragging:
            self._drag_to(event.y)
        else:
            self._update_hover()

    def _on_leave(self, _event) -> None:
        self._pointer = None
        self._update_hover()

    def _on_press(self, event, double: bool = False) -> None:
        self._tip.hide()
        self._pointer = (event.x, event.y)
        self._pressed = None
        card, part = self._locate(event.x, event.y)
        if part == "thumb":
            self._start_drag(event.y)
        elif card is None:
            return
        elif part == "body":
            # Selecting acts on press, like any list. The callbacks redraw the
            # list, so nothing here may touch ``card`` afterwards.
            host = card.host
            if double and self._on_activate is not None:
                self._on_activate(host)
            else:
                self._on_select(host)
        else:
            # Controls act on release, so a press can be dragged off to cancel.
            self._pressed = (card.host, part)

    def _on_release(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self._update_hover()
            self._wake_thumb()
            return
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return
        card, part = self._locate(event.x, event.y)
        if card is None or card.host is not pressed[0] or part != pressed[1]:
            return
        host = card.host
        if part == "switch":
            self._toggle(host)
        elif part == "edit":
            self._on_edit(host)
        elif part == "remove":
            self._on_remove(host)

    def _toggle(self, host: Host) -> None:
        host.include_in_bulk = not host.include_in_bulk
        self._redraw()
        if self._on_toggle is not None:
            self._on_toggle(host)

    def _on_menu(self, event) -> None:
        card, _part = self._locate(event.x, event.y)
        if card is None:
            return
        host = card.host
        self._tip.hide()
        if self._menu is not None:
            self._menu.destroy()
        fleet = "Skip in fleet-wide updates" if host.include_in_bulk else "Include in fleet-wide updates"
        items = [("Select", lambda: self._on_select(host))]
        if self._on_activate is not None and host is not self._connected:
            items.append(("Connect", lambda: self._on_activate(host)))
        items += [
            ("Edit…", lambda: self._on_edit(host)),
            (fleet, lambda: self._toggle(host)),
            None,
            ("Remove…", lambda: self._on_remove(host)),
        ]
        self._menu = _context_menu(self, items)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    # ── Scrolling ───────────────────────────────────────────────────────────

    def _on_configure(self, _event) -> None:
        if (self.winfo_width(), theme.widget_scale(self)) != self._drawn_for:
            self._redraw()
        else:
            self._fit_scroll()
        if self._reveal_pending:
            self._reveal_active()

    def _on_wheel(self, event) -> str:
        self._scroll_to(self._target - event.delta / 120 * self._px(_WHEEL_PX))
        return "break"

    def _max_offset(self) -> int:
        return max(0, self._content - self.winfo_height())

    def _fit_scroll(self) -> None:
        """Re-clamp the scroll position after the content or the view changed size."""
        region = max(self._content, self.winfo_height(), 1)
        self.configure(scrollregion=(0, 0, max(self.winfo_width(), 1), region))
        limit = self._max_offset()
        self._target = min(self._target, limit)
        self._offset = min(self._offset, limit)
        self._show_offset(wake=False)

    def _scroll_to(self, target: float, *, animate: bool = True) -> None:
        self._target = max(0.0, min(float(target), float(self._max_offset())))
        if not animate:
            self._offset = self._target
            self._show_offset()
        elif self._scroll_job is None and self._offset != self._target:
            self._scroll_clock = time.perf_counter()
            self._scroll_step()

    def _scroll_step(self) -> None:
        """One frame of a glide: close a time-based share of the remaining distance."""
        self._scroll_job = None
        now = time.perf_counter()
        elapsed, self._scroll_clock = max(now - self._scroll_clock, 0.008), now
        remaining = self._target - self._offset
        if abs(remaining) < 0.5:
            self._offset = self._target
        else:
            self._offset += remaining * (1 - math.exp(-elapsed / _SCROLL_TAU))
        try:
            self._show_offset()
            if self._offset != self._target:
                self._scroll_job = self.after(_FRAME_MS, self._scroll_step)
        except tk.TclError:
            pass

    def _show_offset(self, *, wake: bool = True) -> None:
        region = max(self._content, self.winfo_height(), 1)
        self._origin = round(self._offset)
        self.yview_moveto(self._origin / region)
        self._place_fades()
        self._place_thumb()
        self._update_hover()
        if wake:
            self._wake_thumb()

    def _place_fades(self) -> None:
        """Soften the top and bottom edges wherever cards continue beyond them.

        Each fade deepens over the first few pixels of scrolling rather than
        switching on, so the list never visibly gains or loses an edge.
        """
        top, bottom = self._fades
        if not top:
            return
        width = max(1, self.winfo_width() - self._px(_GUTTER))
        depth = self._px(_FADE)
        hidden_above = self._origin
        hidden_below = self._max_offset() - self._origin
        for item, hidden, y, downward in (
            (top, hidden_above, self._origin, True),
            (bottom, hidden_below, self._origin + self.winfo_height(), False),
        ):
            if hidden <= 0:
                self.itemconfigure(item, state="hidden")
                continue
            image = theme.fade_image(self, width, min(depth, hidden), theme.SIDEBAR,
                                     downward=downward)
            self.itemconfigure(item, image=image, state="normal")
            self.coords(item, 0, y)
            self.tag_raise(item)

    def _reveal_active(self) -> None:
        """Scroll just far enough that the selected card is fully in view."""
        if self._active is None:
            self._reveal_pending = False
            return
        card = self._by_host.get(id(self._active))
        view = self.winfo_height()
        if card is None or view <= 1:
            return  # Not drawn yet; <Configure> tries again.
        self._reveal_pending = False
        margin = self._px(_GAP)
        if card.top - margin < self._target:
            self._scroll_to(card.top - margin)
        elif card.bottom + margin > self._target + view:
            self._scroll_to(card.bottom + margin - view)

    # ── Scrollbar ───────────────────────────────────────────────────────────
    # An overlay thumb in the gutter beside the cards: invisible until the
    # pointer is over the list or the list is moving, then faded back out.

    def _thumb_span(self) -> tuple[float, float]:
        """The thumb's top and length within the view, in pixels."""
        view = self.winfo_height()
        margin = self._px(3)
        track = max(1, view - 2 * margin)
        length = min(track, max(self._px(_THUMB_MIN), round(track * view / max(self._content, 1))))
        limit = self._max_offset()
        top = margin + (track - length) * (self._offset / limit if limit else 0.0)
        return top, length

    def _place_thumb(self) -> None:
        if not self._thumb:
            return
        if self._max_offset() <= 0:
            self.itemconfigure(self._thumb, state="hidden")
            return
        top, length = self._thumb_span()
        x = self.winfo_width() - self._px(_GUTTER) / 2
        cap = self._px(_THUMB) / 2  # Round caps extend past the line's ends.
        self.coords(self._thumb, x, self._origin + top + cap,
                    x, self._origin + top + length - cap)
        self.itemconfigure(self._thumb, state="normal")
        self.tag_raise(self._thumb)

    def _wake_thumb(self) -> None:
        if self._thumb_job is not None:
            self.after_cancel(self._thumb_job)
            self._thumb_job = None
        if self._max_offset() <= 0:
            return
        grabbed = self._dragging or self._hover[1] == "thumb"
        self._fade_thumb(theme.TEXT_3 if grabbed else theme.BORDER_STRONG)
        if self._pointer is None and not self._dragging:
            self._thumb_job = self.after(900, self._rest_thumb)

    def _rest_thumb(self) -> None:
        self._thumb_job = None
        self._fade_thumb(theme.SIDEBAR, duration=260)

    def _fade_thumb(self, target: str, duration: int = 120) -> None:
        key = ("thumb",)
        self._animations.pop(key, None)
        start = self._thumb_color
        if start == target:
            return

        def apply(progress: float) -> None:
            self._thumb_color = theme.blend(start, target, progress)
            if self._thumb:
                self.itemconfigure(self._thumb, fill=self._thumb_color)

        self._animate(key, duration, apply)

    def _start_drag(self, y: int) -> None:
        top, length = self._thumb_span()
        on_thumb = top <= y <= top + length
        # A click on the bare track brings the thumb's middle to the pointer.
        self._drag_grab = y - top if on_thumb else length / 2
        self._dragging = True
        self._scroll_job_cancel()
        if not on_thumb:
            self._drag_to(y)
        self._update_hover()

    def _drag_to(self, y: int) -> None:
        _top, length = self._thumb_span()
        margin = self._px(3)
        travel = self.winfo_height() - 2 * margin - length
        if travel <= 0:
            return
        fraction = (y - self._drag_grab - margin) / travel
        self._scroll_to(fraction * self._max_offset(), animate=False)

    def _scroll_job_cancel(self) -> None:
        if self._scroll_job is not None:
            self.after_cancel(self._scroll_job)
            self._scroll_job = None
        self._target = self._offset

    # ── Animation ───────────────────────────────────────────────────────────
    # One timer drives every running animation — hover fills, switch slides,
    # the scrollbar fade — so they land in the same frame.

    def _animate(self, key: tuple, duration: int, apply: Callable[[float], None]) -> None:
        """Call ``apply`` with an eased 0–1 over ``duration`` ms. A new animation
        under the same ``key`` replaces the one running."""
        self._animations[key] = (time.perf_counter(), duration / 1000, apply)
        if self._animation_job is None:
            self._animation_job = self.after(_FRAME_MS, self._animation_frame)

    def _animation_frame(self) -> None:
        self._animation_job = None
        now = time.perf_counter()
        try:
            for key, (started, duration, apply) in list(self._animations.items()):
                t = min(1.0, (now - started) / duration)
                if t >= 1.0:
                    del self._animations[key]
                apply(1 - (1 - t) ** 3)
            if self._animations:
                self._animation_job = self.after(_FRAME_MS, self._animation_frame)
        except tk.TclError:
            self._animations.clear()

    def destroy(self) -> None:
        self._tip.hide()
        for job in (self._animation_job, self._scroll_job, self._thumb_job, self._pulse_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._animation_job = self._scroll_job = self._thumb_job = self._pulse_job = None
        self._animations.clear()
        super().destroy()


def _context_menu(parent, items) -> tk.Menu:
    menu = tk.Menu(
        parent, tearoff=0, bg=theme.SURFACE_2, fg=theme.TEXT,
        activebackground=theme.ACCENT, activeforeground="#FFFFFF",
        bd=0, relief="flat", font=(theme.UI_FAMILY, 10),
    )
    for item in items:
        if item is None:
            menu.add_separator()
        else:
            label, command = item
            menu.add_command(label=label, command=command)
    return menu


def _update_summary(host: Host) -> tuple[str, str]:
    """Return the ``(text, colour)`` describing this host's update state."""
    count = host.pending_updates
    if count == UPDATES_UNKNOWN:
        return "", theme.TEXT_3
    if count == UPDATES_FAILED:
        return "⚠  update check failed", theme.DANGER
    if count == 0:
        return "✓  up to date", theme.SUCCESS
    plural = "" if count == 1 else "s"
    return f"↑  {count} update{plural} available", theme.ORANGE


def os_badge_color(host: Optional[Host]) -> str:
    """Badge colour for the header bar's OS chip."""
    if host is None:
        return distro.DEFAULT_BRAND_COLOR
    return distro.brand_color(host.os_info, host.os_like)
