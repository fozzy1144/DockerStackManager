"""Command palette: one search box over every host, stack and action.

Ctrl+K opens it over the main window. Typing filters with a forgiving
subsequence match — "upd med" finds "Update stack media" — and Enter runs the
highlighted entry. The palette knows nothing about the application; the window
hands it a list of :class:`Command` built from whatever is possible right now,
so an action that would be disabled simply is not offered.
"""

import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable, Optional

import customtkinter as ctk

from gui import theme


@dataclass(slots=True)
class Command:
    title: str
    action: Callable[[], None]
    subtitle: str = ""
    group: str = ""
    hint: str = ""
    """Right-aligned text, usually a keyboard shortcut."""
    keywords: str = ""
    """Extra words to match on that are not shown."""
    order: int = field(default=0, compare=False)


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Score how well ``query`` matches ``text``; ``None`` if it does not.

    Every query word must appear, in order, as a subsequence of the text. Matches
    at the start of a word and runs of consecutive characters score higher, so
    "sc" ranks "Scan stacks" above "Disk cleanup".
    """
    query = query.strip().lower()
    if not query:
        return 0
    haystack = text.lower()
    total = 0
    position = 0
    for word in query.split():
        best: Optional[tuple[int, int]] = None
        # Try each place the word's first letter occurs, keeping the best run:
        # taking simply the first occurrence would match "med" as M(anage)-e-d.
        start = haystack.find(word[0], position)
        while start >= 0:
            attempt = _match_from(word, haystack, start)
            if attempt is not None and (best is None or attempt[0] > best[0]):
                best = attempt
            start = haystack.find(word[0], start + 1)
        if best is None:
            return None
        total += best[0]
        position = best[1]
    # Prefer shorter titles when the match quality is equal.
    return total * 100 - len(haystack)


def _match_from(word: str, haystack: str, start: int) -> Optional[tuple[int, int]]:
    """Greedily match ``word`` from ``start``: ``(score, end)`` or ``None``."""
    score = 0
    previous = start - 2
    position = start
    for char in word:
        found = haystack.find(char, position)
        if found < 0:
            return None
        if found == 0 or not haystack[found - 1].isalnum():
            score += 10
        else:
            score += 1
        if found == previous + 1:
            score += 5
        previous = found
        position = found + 1
    return score, position


def rank(query: str, commands: list[Command]) -> list[Command]:
    """``commands`` that match ``query``, best first; original order when empty."""
    if not query.strip():
        return sorted(commands, key=lambda c: c.order)
    scored = []
    for command in commands:
        score = fuzzy_score(query, f"{command.title} {command.subtitle} {command.keywords}")
        if score is None:
            continue
        # A match within the title alone beats one that needs the subtitle.
        title_score = fuzzy_score(query, command.title)
        best = title_score + 50_000 if title_score is not None else score
        scored.append((best, -command.order, command))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [command for _score, _order, command in scored]


class CommandPalette(ctk.CTkToplevel):
    """A borderless search window anchored near the top of its owner."""

    VISIBLE_ROWS = 8
    WIDTH = 640

    def __init__(self, owner, commands: list[Command]):
        super().__init__(owner)
        self._owner = owner
        self._commands = commands
        self._matches: list[Command] = []
        self._selected = 0
        self._top = 0

        self.overrideredirect(True)
        self.configure(fg_color=theme.BORDER_STRONG)
        self.transient(owner)

        shell = ctk.CTkFrame(self, fg_color=theme.SURFACE, corner_radius=0)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        search = ctk.CTkFrame(shell, fg_color="transparent")
        search.pack(fill="x", padx=14, pady=(12, 8))
        theme.search_icon(search, size=16, bg=theme.SURFACE).pack(side="left", padx=(4, 10))
        self._query = tk.StringVar()
        self._entry = ctk.CTkEntry(
            search, textvariable=self._query, height=34, border_width=0,
            fg_color=theme.SURFACE, font=theme.font("title"),
            placeholder_text="Search hosts, stacks and actions…",
        )
        self._entry.pack(side="left", fill="x", expand=True)
        theme.caption(search, "esc", role="caption").pack(side="right")

        theme.divider(shell).pack(fill="x")

        self._list = ctk.CTkFrame(shell, fg_color="transparent")
        self._list.pack(fill="both", expand=True, padx=8, pady=8)
        self._rows: list[_Row] = [_Row(self._list, self._run_index, i)
                                  for i in range(self.VISIBLE_ROWS)]
        self._empty = theme.caption(self._list, "No matches.", role="small")

        theme.divider(shell).pack(fill="x")
        footer = ctk.CTkFrame(shell, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=7)
        theme.caption(footer, "↑↓ to move   ·   Enter to run").pack(side="left")
        self._count = theme.caption(footer, "")
        self._count.pack(side="right")

        self._query.trace_add("write", lambda *_: self._refresh(reset=True))
        for sequence, handler in (
            ("<Escape>", lambda _e: self.close()),
            ("<Return>", lambda _e: self._run_index(self._selected - self._top)),
            ("<Down>", lambda _e: self._move(1)),
            ("<Up>", lambda _e: self._move(-1)),
            ("<Next>", lambda _e: self._move(self.VISIBLE_ROWS)),
            ("<Prior>", lambda _e: self._move(-self.VISIBLE_ROWS)),
        ):
            self.bind(sequence, handler)
        self.bind("<Button-1>", self._click_outside, add="+")

        self._refresh(reset=True)
        self._position()
        theme.round_corners(self)
        theme.fade_in(self, duration=100)
        self.after(40, self._grab)

    # ── Window ──────────────────────────────────────────────────────────────

    def _position(self) -> None:
        self.update_idletasks()
        scale = theme.widget_scale(self)
        width = round(self.WIDTH * scale)
        height = self.winfo_reqheight()
        owner = self._owner
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + round(90 * scale)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _grab(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self._entry.focus_set()
            self.grab_set()
        except tk.TclError:
            pass

    def _click_outside(self, event) -> None:
        x, y = event.x_root, event.y_root
        left, top = self.winfo_rootx(), self.winfo_rooty()
        if not (left <= x < left + self.winfo_width() and top <= y < top + self.winfo_height()):
            self.close()

    def close(self) -> None:
        try:
            self.grab_release()
            self.destroy()
        except tk.TclError:
            pass

    # ── List ────────────────────────────────────────────────────────────────

    def _refresh(self, reset: bool = False) -> None:
        if reset:
            self._matches = rank(self._query.get(), self._commands)
            self._selected = 0
            self._top = 0
        visible = self._matches[self._top:self._top + self.VISIBLE_ROWS]
        for index, row in enumerate(self._rows):
            if index < len(visible):
                row.show(visible[index], selected=self._top + index == self._selected)
            else:
                row.hide()
        if visible:
            self._empty.pack_forget()
        else:
            self._empty.pack(anchor="w", padx=10, pady=12)
        total = len(self._matches)
        self._count.configure(text=f"{total} result{'' if total == 1 else 's'}")

    def _move(self, delta: int) -> str:
        if not self._matches:
            return "break"
        self._selected = max(0, min(len(self._matches) - 1, self._selected + delta))
        if self._selected < self._top:
            self._top = self._selected
        elif self._selected >= self._top + self.VISIBLE_ROWS:
            self._top = self._selected - self.VISIBLE_ROWS + 1
        self._refresh()
        return "break"

    def _run_index(self, visible_index: int) -> None:
        index = self._top + visible_index
        if not 0 <= index < len(self._matches):
            return
        command = self._matches[index]
        self.close()
        # Run after the palette has gone, so a dialog the command opens gets focus.
        self._owner.after(10, command.action)


class _Row(ctk.CTkFrame):
    """One result line: title, subtitle, and a right-aligned hint or group."""

    def __init__(self, parent, on_run: Callable[[int], None], index: int):
        super().__init__(parent, fg_color=theme.SURFACE, corner_radius=8, height=44)
        self.grid_columnconfigure(1, weight=1)
        self._bar = tk.Frame(self, width=3, bg=theme.SURFACE, bd=0, highlightthickness=0)
        self._bar.grid(row=0, column=0, rowspan=2, sticky="ns", pady=8, padx=(4, 0))
        self._title = theme.text(self, "", "body_strong")
        self._title.grid(row=0, column=1, sticky="w", padx=(10, 8), pady=(6, 0))
        self._subtitle = theme.caption(self, "", role="caption")
        self._subtitle.grid(row=1, column=1, sticky="w", padx=(10, 8), pady=(0, 6))
        self._hint = ctk.CTkLabel(self, text="", height=22, corner_radius=6,
                                  font=theme.font("caption"), text_color=theme.TEXT_3)
        self._hint.grid(row=0, column=2, rowspan=2, padx=(4, 12))
        theme.bind_click(self, lambda: on_run(index))

    def show(self, command: Command, *, selected: bool) -> None:
        bg = theme.tint(theme.ACCENT, 0.16, theme.SURFACE) if selected else theme.SURFACE
        self.configure(fg_color=bg)
        self._bar.configure(bg=theme.ACCENT if selected else bg)
        self._title.configure(text=command.title, text_color=theme.TEXT)
        self._subtitle.configure(text=command.subtitle)
        if command.subtitle:
            self._subtitle.grid()
        else:
            self._subtitle.grid_remove()
        hint = command.hint or command.group
        self._hint.configure(
            text=f"  {hint}  " if hint else "",
            fg_color=theme.SURFACE_3 if command.hint else bg,
            text_color=theme.TEXT_2 if command.hint else theme.TEXT_3,
        )
        if not self.winfo_ismapped():
            self.pack(fill="x", pady=1)

    def hide(self) -> None:
        self.pack_forget()
