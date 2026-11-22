"""Themed replacements for :mod:`tkinter.messagebox`, plus in-window toasts.

Native message boxes are drawn by the OS in its own light style, which breaks
the window's look at exactly the moments that matter — confirming something
destructive, or reading why something failed. These keep the same call shape as
``messagebox`` (``askyesno(title, message, parent=...)``) so call sites read the
same, and add what the native ones cannot: a verb on the confirm button, a
danger tone, and a scrollable block for long output such as a Docker error.

:func:`toast` is for outcomes that need no decision — "Connected", "Saved" — and
slides into the corner of the window instead of demanding a click.
"""

import tkinter as tk
from typing import Optional

import customtkinter as ctk

from gui import theme

#: tone -> (glyph, colour)
_TONES = {
    "info": ("i", theme.INFO),
    "question": ("?", theme.ACCENT_TEXT),
    "success": ("✓", theme.SUCCESS),
    "warning": ("!", theme.WARNING),
    "danger": ("!", theme.DANGER),
    "error": ("✕", theme.DANGER),
}

_DETAIL_LINES = 8
"""Messages longer than this many lines move their tail into a scroll box."""


def split_message(message: str, limit: int = _DETAIL_LINES) -> tuple[str, str]:
    """Split ``message`` into a lead paragraph and a detail block.

    The first paragraph is the sentence a person reads; anything long after it —
    a list of images, a compiler-style error — goes into the scrollable detail
    box so the dialog keeps a sane height.
    """
    text = (message or "").strip()
    if text.count("\n") < limit:
        return text, ""
    lead, _, rest = text.partition("\n\n")
    if not rest:
        lines = text.splitlines()
        return lines[0], "\n".join(lines[1:]).strip()
    return lead.strip(), rest.strip()


class _Dialog(ctk.CTkToplevel):
    """A modal message with up to two buttons. ``result`` is True on confirm."""

    def __init__(
        self,
        parent,
        title: str,
        message: str,
        *,
        tone: str,
        confirm: str,
        cancel: Optional[str],
        default_cancel: bool = False,
    ):
        owner = parent.winfo_toplevel() if parent is not None else None
        super().__init__(owner)
        self.result = False
        self.title(title)
        self.resizable(False, False)
        theme.style_window(self)
        if owner is not None:
            self.transient(owner)

        glyph, color = _TONES.get(tone, _TONES["info"])
        lead, detail = split_message(message)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=24, pady=(22, 0))
        body.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            body, text=glyph, width=36, height=36, corner_radius=10,
            fg_color=theme.tint(color, 0.16, theme.BG), text_color=color,
            font=theme.font("title"),
        ).grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 14))

        theme.heading(body, title, role="title").grid(row=0, column=1, sticky="w")
        theme.caption(
            body, lead, role="body", color=theme.TEXT_2, wraplength=400,
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))

        if detail:
            box = ctk.CTkTextbox(
                body, height=min(220, 22 + 18 * detail.count("\n")), width=400,
                font=theme.mono_tuple(12), wrap="word", fg_color=theme.INSET,
                border_width=1, border_color=theme.BORDER, border_spacing=8,
            )
            box.insert("1.0", detail)
            box.configure(state="disabled")
            theme.style_textbox(box)
            box.grid(row=2, column=1, sticky="ew", pady=(12, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="e", padx=24, pady=(22, 20))
        variant = "danger" if tone == "danger" else "primary"
        self._confirm = theme.button(buttons, confirm, self._accept, variant=variant)
        if cancel:
            self._cancel = theme.button(buttons, cancel, self._reject, variant="ghost")
            self._cancel.pack(side="left", padx=(0, 8))
        self._confirm.pack(side="left")

        # Destructive questions default to the safe answer on Enter.
        self.bind("<Return>", lambda _e: self._reject() if (cancel and default_cancel) else self._accept())
        self.bind("<Escape>", lambda _e: self._reject())
        self.protocol("WM_DELETE_WINDOW", self._reject)

        self.update_idletasks()
        width = max(460, self.winfo_reqwidth() // max(theme.widget_scale(self), 1))
        height = self.winfo_reqheight() / max(theme.widget_scale(self), 1)
        if owner is not None:
            theme.center_on(self, owner, int(width), int(height))
        self.grab_set()
        self.after(60, self._focus)

    def _focus(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _accept(self) -> None:
        self.result = True
        self._close()

    def _reject(self) -> None:
        self.result = False
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def _show(parent, title, message, **kwargs) -> bool:
    dialog = _Dialog(parent, title, message, **kwargs)
    try:
        dialog.wait_window()
    except tk.TclError:
        pass
    return dialog.result


# ── messagebox-compatible API ───────────────────────────────────────────────

def askyesno(title: str, message: str, parent=None, *, confirm: str = "Continue",
             cancel: str = "Cancel", tone: str = "question") -> bool:
    """Ask a question. ``tone="danger"`` paints the confirm button red and makes
    Enter choose Cancel, so a reflexive keypress cannot destroy anything."""
    return _show(parent, title, message, tone=tone, confirm=confirm, cancel=cancel,
                 default_cancel=tone == "danger")


def showinfo(title: str, message: str, parent=None, *, button: str = "OK") -> None:
    _show(parent, title, message, tone="info", confirm=button, cancel=None)


def showwarning(title: str, message: str, parent=None, *, button: str = "OK") -> None:
    _show(parent, title, message, tone="warning", confirm=button, cancel=None)


def showerror(title: str, message: str, parent=None, *, button: str = "OK") -> None:
    _show(parent, title, message, tone="error", confirm=button, cancel=None)


# ──────────────────────────────────────────────────────────────────────────────
# Toasts
# ──────────────────────────────────────────────────────────────────────────────

_TOAST_MS = 4200
_TOAST_GAP = 10


def toast(window: tk.Misc, message: str, tone: str = "success", *, detail: str = "") -> None:
    """Slide a short notice into the bottom-right corner of ``window``.

    In-window rather than a separate OS window: it moves with the app, never
    lands on another monitor, and cannot steal focus from what you are typing.
    """
    try:
        root = window.winfo_toplevel()
        _Toast(root, message, tone, detail)
    except tk.TclError:
        pass


class _Toast(ctk.CTkFrame):
    def __init__(self, root, message: str, tone: str, detail: str):
        super().__init__(
            root, fg_color=theme.SURFACE_2, corner_radius=12, border_width=1,
            border_color=theme.BORDER_STRONG,
        )
        self._window = root
        glyph, color = _TONES.get(tone, _TONES["info"])

        ctk.CTkLabel(
            self, text=glyph, width=26, height=26, corner_radius=8,
            fg_color=theme.tint(color, 0.18, theme.SURFACE_2), text_color=color,
            font=theme.font("small_strong"),
        ).grid(row=0, column=0, rowspan=2, padx=(14, 10), pady=12, sticky="n")
        theme.text(self, message, "body_strong").grid(
            row=0, column=1, sticky="w", padx=(0, 16), pady=(12, 0 if detail else 12)
        )
        if detail:
            theme.caption(self, detail, role="small", wraplength=300).grid(
                row=1, column=1, sticky="w", padx=(0, 16), pady=(0, 12)
            )
        theme.bind_click(self, self.dismiss)

        stack = _toasts(root)
        stack.append(self)
        self._target = 0.0
        self._place_all(skip=self)
        self._slide(0)
        self._job: Optional[str] = self.after(_TOAST_MS, self.dismiss)

    def _place_all(self, skip=None) -> None:
        """Stack every toast upward from the corner, newest at the bottom."""
        bottom = 24
        for item in reversed(_toasts(self._window)):
            item.update_idletasks()
            item._target = bottom
            bottom += item.winfo_reqheight() / max(theme.widget_scale(item), 1) + _TOAST_GAP
            if item is not skip:
                item._move(item._target)

    def _move(self, y: float) -> None:
        self.place(relx=1.0, rely=1.0, anchor="se", x=-24, y=-y)
        self.lift()

    def _slide(self, step: int, steps: int = 7) -> None:
        """Ease in from below the window edge."""
        t = (step + 1) / steps
        eased = 1 - (1 - t) ** 3
        start = -60
        self._move(start + (self._target - start) * eased)
        if step + 1 < steps:
            self.after(16, lambda: self._slide(step + 1, steps))

    def dismiss(self) -> None:
        stack = _toasts(self._window)
        if self in stack:
            stack.remove(self)
        try:
            if self._job:
                self.after_cancel(self._job)
            self.destroy()
        except tk.TclError:
            return
        if stack:
            stack[-1]._place_all()


def _toasts(root) -> list:
    if not hasattr(root, "_dsm_toasts"):
        root._dsm_toasts = []
    return root._dsm_toasts
