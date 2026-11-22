"""Colour-coded output log that is safe to write to from worker threads."""

import queue
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog

import customtkinter as ctk

from gui import dialogs, theme

LEVEL_COLORS = {
    "info": "#C3C9D5",
    "success": theme.SUCCESS,
    "warn": theme.WARNING,
    "error": theme.DANGER,
}
_DEFAULT_LEVEL = "info"

_STAMP = "%H:%M:%S"
_STAMP_WIDTH = 8
"""Characters in a rendered timestamp; the flush splits each line here."""
_GAP = "   "
_SOURCE_GAP = "  "
"""Between a line's source column and its message."""

#: Filter name -> levels it hides. Lines are hidden with Tk's ``elide`` tag
#: option, so switching filters is instant and nothing is lost.
FILTERS = {
    "All": frozenset(),
    "Issues": frozenset({"info", "success"}),
    "Errors": frozenset({"info", "success", "warn"}),
}


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

    def __init__(self, parent, title: str = "Activity", filters: bool = True, **kwargs):
        options = dict(fg_color=theme.SURFACE, corner_radius=12, border_width=1,
                       border_color=theme.BORDER)
        options.update(kwargs)
        super().__init__(parent, **options)

        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._flush_job: str | None = None
        self._source_tags: set[str] = set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=(16, 10), pady=(12, 8))
        theme.heading(header, title, role="heading").pack(side="left")
        self.toolbar = ctk.CTkFrame(header, fg_color="transparent", height=28)
        """Callers may add their own controls here, between title and buttons."""
        theme.button(header, "Clear", self.clear, variant="ghost", size="sm").pack(
            side="right"
        )
        theme.button(header, "Save…", self.save, variant="ghost", size="sm").pack(
            side="right", padx=(0, 2)
        )
        # Colours come from the global theme (see theme.apply).
        self._filter = ctk.CTkSegmentedButton(
            header, values=list(FILTERS), command=self.set_filter, height=26,
            font=theme.font("caption"), corner_radius=7, text_color=theme.TEXT_2,
        )
        self._filter.set("All")
        if filters:
            self._filter.pack(side="right", padx=(0, 8))
        # Packed last: when the panel is narrow the packer runs out of room at
        # the end of the list, and the flexible toolbar is the part to squeeze.
        self.toolbar.pack(side="left", fill="x", expand=True, padx=(14, 0))

        self._text = ctk.CTkTextbox(
            self,
            state="disabled",
            font=theme.mono_tuple(13),
            wrap="word",
            fg_color=theme.INSET,
            corner_radius=8,
            border_spacing=10,
            height=120,
            activate_scrollbars=True,
        )
        self._text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        theme.style_textbox(self._text)

        # Wrapped continuation lines hang under the message, not the timestamp.
        self._measure = tkfont.Font(font=theme.scaled_mono(self, 13)).measure
        indent = self._measure("0" * _STAMP_WIDTH + _GAP)
        for level, color in LEVEL_COLORS.items():
            self._text.tag_config(level, foreground=color, lmargin2=indent)
            # Each level has its own timestamp tag so a filter hides both halves.
            self._text.tag_config(f"ts_{level}", foreground=theme.TEXT_3, lmargin2=indent)

        self._schedule_flush()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def log(self, message: str, level: str = _DEFAULT_LEVEL, *,
            source: str = "", color: str = "") -> None:
        """Queue a message for display. Safe to call from any thread.

        ``source`` names where the line came from — a host, during a bulk run —
        and is shown between the timestamp and the message in ``color``, so the
        interleaved output of several hosts can be told apart at a glance. The
        message itself keeps its level colour: red still means an error.
        """
        if level not in LEVEL_COLORS:
            level = _DEFAULT_LEVEL
        stamp = datetime.now().strftime(_STAMP)
        text = str(message)
        mark = f"{source}{_SOURCE_GAP}" if source else ""
        for line in text.splitlines() or [""]:
            # Blank lines stay blank — a timestamp on a separator is just noise.
            if line:
                self._queue.put((f"{stamp}{_GAP}{line}\n", level, mark, color))
            else:
                self._queue.put(("\n", level))

    def set_filter(self, name: str) -> None:
        """Show only the levels ``name`` keeps (see :data:`FILTERS`)."""
        hidden = FILTERS.get(name, frozenset())
        inner = self._text._textbox
        for level in LEVEL_COLORS:
            inner.tag_configure(level, elide=level in hidden)
            inner.tag_configure(f"ts_{level}", elide=level in hidden)
        inner.see("end")

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
            dialogs.showerror("Could not save the log", str(exc), parent=self)
            return
        dialogs.toast(self, "Log saved", detail=path)

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
            inner = self._text._textbox
            # Follow new output only when already at the bottom: someone scrolled
            # up to read an earlier error should not be yanked away from it.
            at_end = inner.yview()[1] >= 0.999
            inner.configure(state="normal")
            # One insert call per flush, alternating text and tags.
            chunks: list = []
            for text, level, *source in items:
                if text == "\n":
                    chunks += [text, ()]
                    continue
                head = _STAMP_WIDTH + len(_GAP)
                mark, color = source if source else ("", "")
                chunks += [text[:_STAMP_WIDTH], (f"ts_{level}",)]
                if mark:
                    # Tagged with the level as well, so a filter hides the whole line.
                    hang = self._hang_tag(len(mark))
                    chunks += [text[_STAMP_WIDTH:head], (level,),
                               mark, (level, self._source_tag(color)),
                               text[head:], (level, hang)]
                else:
                    chunks += [text[_STAMP_WIDTH:], (level,)]
            inner.insert("end", *chunks)
            self._trim()
            inner.configure(state="disabled")
            if at_end:
                inner.see("end")
        self._schedule_flush()

    def _source_tag(self, color: str) -> str:
        """The text tag that paints a source column in ``color``, made on first use.

        Created after the level tags, so it outranks them: the source keeps its
        own colour whatever the level of the line it introduces.
        """
        tag = f"src_{color or theme.TEXT_2}"
        if tag not in self._source_tags:
            self._source_tags.add(tag)
            self._text._textbox.tag_configure(tag, foreground=color or theme.TEXT_2)
        return tag

    def _hang_tag(self, columns: int) -> str:
        """The tag that makes a sourced message wrap under itself, not under its
        source: a hanging indent ``columns`` characters deeper than usual."""
        tag = f"hang_{columns}"
        if tag not in self._source_tags:
            self._source_tags.add(tag)
            depth = self._measure("0" * (_STAMP_WIDTH + columns) + _GAP)
            self._text._textbox.tag_configure(tag, lmargin2=depth)
        return tag

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
