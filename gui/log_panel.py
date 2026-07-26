"""Colour-coded output log that is safe to write to from worker threads."""

import queue
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

LEVEL_COLORS = {
    "info": "#A0D8EF",
    "success": "#90EE90",
    "warn": "#FFD700",
    "error": "#FF7F7F",
}
_DEFAULT_LEVEL = "info"


class LogPanel(ctk.CTkFrame):
    """A scrolling log view fed through a queue.

    :meth:`log` only enqueues; a single Tk timer drains the queue and performs
    one batched insert per tick. That matters because remote updates arrive line
    by line — ``apt`` alone can emit thousands — and touching the widget once
    per line is what made the window stutter. Enqueuing is also what makes the
    method callable directly from a background thread, so callers no longer need
    to marshal every line through ``after()`` themselves.
    """

    MAX_LINES = 4000
    """Oldest lines are dropped past this, to bound memory and redraw cost."""

    _FLUSH_MS = 100
    _MAX_PER_FLUSH = 500

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)

        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._flush_job: str | None = None

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(header, text="Output Log", font=ctk.CTkFont(weight="bold")).pack(
            side="left"
        )
        ctk.CTkButton(header, text="Clear", width=60, height=24, command=self.clear).pack(
            side="right", padx=(4, 0)
        )
        ctk.CTkButton(header, text="Save…", width=60, height=24, command=self.save).pack(
            side="right"
        )

        self._text = ctk.CTkTextbox(
            self, state="disabled", font=("Consolas", 12), wrap="word"
        )
        self._text.pack(fill="both", expand=True, padx=8, pady=6)

        for level, color in LEVEL_COLORS.items():
            self._text.tag_config(level, foreground=color)

        self._schedule_flush()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def log(self, message: str, level: str = _DEFAULT_LEVEL) -> None:
        """Queue a message for display. Safe to call from any thread."""
        if level not in LEVEL_COLORS:
            level = _DEFAULT_LEVEL
        stamp = datetime.now().strftime("%H:%M:%S")
        text = str(message)
        for line in text.splitlines() or [""]:
            # Blank lines stay blank — a timestamp on a separator is just noise.
            self._queue.put((f"[{stamp}] {line}\n" if line else "\n", level))

    def clear(self) -> None:
        """Discard everything displayed, and anything still queued."""
        self._discard_queued()
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def save(self) -> None:
        """Write the visible log to a file, for keeping a record of an update."""
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save log",
            defaultextension=".log",
            initialfile=f"docker-stack-manager-{datetime.now():%Y%m%d-%H%M%S}.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)

    # ──────────────────────────────────────────────────────────────────────────
    # Queue draining
    # ──────────────────────────────────────────────────────────────────────────

    def _drain(self) -> list[tuple[str, str]]:
        """Pop up to ``_MAX_PER_FLUSH`` queued lines, oldest first."""
        items: list[tuple[str, str]] = []
        for _ in range(self._MAX_PER_FLUSH):
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    def _discard_queued(self) -> int:
        """Throw away every queued line, however many there are.

        Clearing has to be unbounded, unlike a flush: an ``apt`` run can queue
        thousands of lines, and draining only one flush's worth meant Clear
        emptied the view and then watched the backlog scroll straight back in.
        """
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return discarded
            discarded += 1

    def _flush(self) -> None:
        items = self._drain()
        if items:
            self._text.configure(state="normal")
            # Consecutive lines at the same level become a single insert.
            for text, level in _coalesce(items):
                self._text.insert("end", text, level)
            self._trim()
            self._text.configure(state="disabled")
            self._text.see("end")
        self._schedule_flush()

    def _trim(self) -> None:
        """Drop the oldest lines once the buffer exceeds :attr:`MAX_LINES`."""
        last_line = int(self._text.index("end-1c").split(".")[0])
        excess = last_line - self.MAX_LINES
        if excess > 0:
            self._text.delete("1.0", f"{excess + 1}.0")

    def _schedule_flush(self) -> None:
        try:
            self._flush_job = self.after(self._FLUSH_MS, self._flush)
        except tk.TclError:
            self._flush_job = None  # Window is going away.

    def destroy(self) -> None:
        if self._flush_job is not None:
            try:
                self.after_cancel(self._flush_job)
            except tk.TclError:
                pass
            self._flush_job = None
        super().destroy()


def _coalesce(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge runs of same-level lines into one ``(text, level)`` chunk each."""
    merged: list[tuple[str, str]] = []
    for text, level in items:
        if merged and merged[-1][1] == level:
            merged[-1] = (merged[-1][0] + text, level)
        else:
            merged.append((text, level))
    return merged
