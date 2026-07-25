"""A YAML-aware code editor widget: line numbers, highlighting, and find/replace.

Built on a raw :class:`tkinter.Text` rather than ``CTkTextbox`` because an editor
needs the things the wrapper hides — tag configuration, the undo stack, and
``dlineinfo`` for gutter alignment.

The colours are VS Code's Dark+ palette. That is a deliberate choice rather than
a theme-matching one: anyone editing YAML has seen it, and familiarity is worth
more here than blending into the surrounding window.
"""

import re
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

# ── Palette ──────────────────────────────────────────────────────────────────
BG = "#1E1E1E"
FG = "#D4D4D4"
GUTTER_BG = "#1E1E1E"
GUTTER_FG = "#858585"
GUTTER_ACTIVE_FG = "#C6C6C6"
SELECT_BG = "#264F78"
CURSOR = "#AEAFAD"

SYNTAX_COLORS = {
    "key": "#9CDCFE",
    "string": "#CE9178",
    "number": "#B5CEA8",
    "literal": "#569CD6",
    "comment": "#6A9955",
    "variable": "#DCDCAA",
    "anchor": "#4EC9B0",
    "punctuation": "#808080",
}

MATCH_BG = "#515C6A"
CURRENT_MATCH_BG = "#9E6A03"
ERROR_LINE_BG = "#4B1818"

INDENT = "  "
"""Two spaces. YAML forbids tabs for indentation, so the editor never inserts one."""

# ── Syntax patterns, applied to the non-comment part of each line ────────────
_KEY_RE = re.compile(r"^(\s*(?:-\s+)?)([\w.\-/]+)(\s*:)")
_STRING_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'")
_VARIABLE_RE = re.compile(r"\$\{[^}]*\}")
_ANCHOR_RE = re.compile(r"(?<![\w])[&*][\w.\-]+")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_LITERAL_RE = re.compile(
    r"(?<![\w-])(?:true|false|yes|no|on|off|null|~)(?![\w-])", re.IGNORECASE
)
_LIST_MARK_RE = re.compile(r"^\s*(-)(?=\s|$)")


class CodeEditor(ctk.CTkFrame):
    """Editable YAML view with a line-number gutter and live highlighting."""

    def __init__(self, parent, on_change: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(parent, fg_color=BG, corner_radius=6, **kwargs)
        self._on_change = on_change
        self._highlight_job: Optional[str] = None
        self._match_count = 0
        self._current_match = 0

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._gutter = tk.Canvas(
            self, width=52, bg=GUTTER_BG, highlightthickness=0, takefocus=0
        )
        self._gutter.grid(row=0, column=0, sticky="ns")

        self.text = _ProxiedText(
            self,
            wrap="none",
            undo=True,
            maxundo=-1,
            autoseparators=True,
            font=("Consolas", 12),
            bg=BG,
            fg=FG,
            insertbackground=CURSOR,
            selectbackground=SELECT_BG,
            selectforeground=FG,
            relief="flat",
            padx=8,
            pady=6,
            tabs="1c",
        )
        self.text.grid(row=0, column=1, sticky="nsew")

        y_scroll = ctk.CTkScrollbar(self, command=self.text.yview)
        y_scroll.grid(row=0, column=2, sticky="ns")
        x_scroll = ctk.CTkScrollbar(
            self, orientation="horizontal", command=self.text.xview
        )
        x_scroll.grid(row=1, column=1, sticky="ew")
        self.text.configure(
            yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set
        )

        for name, color in SYNTAX_COLORS.items():
            self.text.tag_config(name, foreground=color)
        self.text.tag_config("match", background=MATCH_BG)
        self.text.tag_config("current_match", background=CURRENT_MATCH_BG)
        self.text.tag_config("error_line", background=ERROR_LINE_BG)
        # Comments win over anything a regex found inside them.
        self.text.tag_raise("comment")
        self.text.tag_raise("match")
        self.text.tag_raise("current_match")

        self.text.bind("<<Change>>", self._on_text_change)
        # A resize only moves the numbers; it must NOT run the on_change
        # callback. Doing so was a feedback loop: the owner's status bar text
        # changed width, the layout reflowed, the editor got <Configure> again,
        # and Tk never drained its event queue.
        self.text.bind("<Configure>", lambda _event: self._redraw_gutter())
        self.text.bind("<Tab>", self._on_tab)
        self.text.bind("<Shift-Tab>", self._on_shift_tab)
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<Control-a>", self._select_all)
        self.text.bind("<Control-A>", self._select_all)
        self.text.bind("<Control-y>", lambda _e: self.redo())
        self.text.bind("<Control-Y>", lambda _e: self.redo())

    # ── Content ──────────────────────────────────────────────────────────────

    def get_text(self) -> str:
        """Current buffer contents, always ending in exactly one newline."""
        return self.text.get("1.0", "end-1c").rstrip("\n") + "\n"

    def set_text(self, content: str, *, mark_clean: bool = True) -> None:
        """Replace the buffer. Clears the undo history when marking clean."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.mark_set("insert", "1.0")
        self.text.see("1.0")
        if mark_clean:
            self.text.edit_reset()
            self.text.edit_modified(False)
        self._highlight_now()
        self._redraw_gutter()

    @property
    def modified(self) -> bool:
        return bool(self.text.edit_modified())

    def mark_clean(self) -> None:
        self.text.edit_modified(False)

    def undo(self) -> None:
        try:
            self.text.edit_undo()
        except tk.TclError:
            pass  # Nothing left to undo.

    def redo(self) -> None:
        try:
            self.text.edit_redo()
        except tk.TclError:
            pass

    def cursor_position(self) -> tuple[int, int]:
        """1-based line and column of the insertion point."""
        line, column = self.text.index("insert").split(".")
        return int(line), int(column) + 1

    def line_count(self) -> int:
        return int(self.text.index("end-1c").split(".")[0])

    def current_indent(self) -> int:
        """Leading spaces on the cursor's line, used to place snippets."""
        line = self.text.get("insert linestart", "insert lineend")
        return len(line) - len(line.lstrip(" "))

    def goto_line(self, line: int) -> None:
        """Move the cursor to a line and scroll it into view."""
        line = max(1, min(line, self.line_count()))
        self.text.mark_set("insert", f"{line}.0")
        self.text.see(f"{line}.0")
        self.text.focus_set()
        self._redraw_gutter()

    def insert_block(self, block: str) -> None:
        """Insert a multi-line block on its own line below the cursor."""
        line, _ = self.cursor_position()
        current = self.text.get(f"{line}.0", f"{line}.end")
        target = f"{line}.end" if current.strip() else f"{line}.0"

        if current.strip():
            self.text.insert(target, "\n" + block.rstrip("\n"))
        else:
            self.text.insert(target, block.rstrip("\n") + "\n")

        self.text.edit_separator()
        self._highlight_now()
        self._redraw_gutter()
        self.text.see("insert")

    def highlight_error_lines(self, lines: list[int]) -> None:
        """Tint the given lines, so lint findings are visible in the text."""
        self.text.tag_remove("error_line", "1.0", "end")
        for line in lines:
            if 1 <= line <= self.line_count():
                self.text.tag_add("error_line", f"{line}.0", f"{line}.end+1c")

    # ── Editing behaviour ────────────────────────────────────────────────────

    def _on_tab(self, _event) -> str:
        """Insert two spaces, or indent every line of the selection."""
        if self._has_selection():
            self._shift_selection(indent=True)
            return "break"
        self.text.insert("insert", INDENT)
        return "break"

    def _on_shift_tab(self, _event) -> str:
        if self._has_selection():
            self._shift_selection(indent=False)
            return "break"
        line_start = self.text.get("insert linestart", "insert")
        if line_start.endswith(INDENT):
            self.text.delete(f"insert-{len(INDENT)}c", "insert")
        return "break"

    def _on_return(self, _event) -> str:
        """Newline that keeps the current indent, deepening it after a ``key:``.

        Hand-indenting YAML is where most syntax errors come from, so the editor
        does the obvious part automatically.
        """
        line = self.text.get("insert linestart", "insert")
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if stripped.endswith(":") and not stripped.startswith("#"):
            indent += len(INDENT)
        elif stripped.startswith("- ") and len(stripped) > 2:
            indent += 2  # Align with the content after the dash.

        self.text.insert("insert", "\n" + " " * indent)
        self.text.see("insert")
        return "break"

    def _select_all(self, _event) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _has_selection(self) -> bool:
        return bool(self.text.tag_ranges("sel"))

    def _shift_selection(self, *, indent: bool) -> None:
        first = int(str(self.text.index("sel.first")).split(".")[0])
        last = int(str(self.text.index("sel.last")).split(".")[0])
        for line in range(first, last + 1):
            if indent:
                self.text.insert(f"{line}.0", INDENT)
            else:
                start = self.text.get(f"{line}.0", f"{line}.{len(INDENT)}")
                if start == INDENT:
                    self.text.delete(f"{line}.0", f"{line}.{len(INDENT)}")
        self.text.tag_add("sel", f"{first}.0", f"{last}.end")
        self.text.edit_separator()

    # ── Highlighting ─────────────────────────────────────────────────────────

    def _on_text_change(self, _event=None) -> None:
        self._redraw_gutter()
        self._schedule_highlight()
        if self._on_change is not None:
            self._on_change()

    def _schedule_highlight(self) -> None:
        if self._highlight_job is not None:
            try:
                self.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
        try:
            self._highlight_job = self.after(90, self._highlight_now)
        except tk.TclError:
            self._highlight_job = None

    def _highlight_now(self) -> None:
        """Re-tag the whole buffer.

        Whole-buffer work is fine here: compose files are small, and doing it in
        one pass avoids the bookkeeping of tracking which lines changed.
        """
        self._highlight_job = None
        for name in SYNTAX_COLORS:
            self.text.tag_remove(name, "1.0", "end")

        content = self.text.get("1.0", "end-1c")
        for number, line in enumerate(content.split("\n"), start=1):
            if line.strip():
                self._highlight_line(number, line)

    def _highlight_line(self, number: int, line: str) -> None:
        code, comment_at = _split_comment(line)

        if comment_at is not None:
            self._tag(number, comment_at, len(line), "comment")

        match = _KEY_RE.match(code)
        if match:
            self._tag(number, match.start(2), match.end(2), "key")
            self._tag(number, match.start(3), match.end(3), "punctuation")

        list_mark = _LIST_MARK_RE.match(code)
        if list_mark:
            self._tag(number, list_mark.start(1), list_mark.end(1), "punctuation")

        # Strings last among the value patterns so quoted numbers stay strings.
        for pattern, tag in (
            (_NUMBER_RE, "number"),
            (_LITERAL_RE, "literal"),
            (_ANCHOR_RE, "anchor"),
            (_VARIABLE_RE, "variable"),
            (_STRING_RE, "string"),
        ):
            for found in pattern.finditer(code):
                self._tag(number, found.start(), found.end(), tag)

    def _tag(self, line: int, start: int, end: int, tag: str) -> None:
        self.text.tag_add(tag, f"{line}.{start}", f"{line}.{end}")

    # ── Gutter ───────────────────────────────────────────────────────────────

    def _redraw_gutter(self) -> None:
        """Draw a number beside each visible line.

        Iterating line numbers rather than stepping an index with ``+1line`` is
        deliberate: Tk clamps that arithmetic at the last line, so the index stops
        advancing and the loop never ends. Counting to :meth:`line_count` cannot
        run away.
        """
        self._gutter.delete("all")
        cursor_line, _ = self.cursor_position()
        width = self._gutter.winfo_width()
        first_visible = int(self.text.index("@0,0").split(".")[0])

        for line_number in range(first_visible, self.line_count() + 1):
            info = self.text.dlineinfo(f"{line_number}.0")
            if info is None:
                break  # Below the bottom of the viewport, or not yet mapped.
            self._gutter.create_text(
                width - 8,
                info[1],
                anchor="ne",
                text=str(line_number),
                font=("Consolas", 11),
                fill=GUTTER_ACTIVE_FG if line_number == cursor_line else GUTTER_FG,
            )

    # ── Search ───────────────────────────────────────────────────────────────

    def find_all(self, needle: str, *, case_sensitive: bool = False) -> int:
        """Tag every occurrence and return the count."""
        self.text.tag_remove("match", "1.0", "end")
        self.text.tag_remove("current_match", "1.0", "end")
        self._match_count = 0
        self._current_match = 0
        if not needle:
            return 0

        start = "1.0"
        while True:
            position = self.text.search(
                needle, start, stopindex="end", nocase=not case_sensitive
            )
            if not position:
                break
            end = f"{position}+{len(needle)}c"
            self.text.tag_add("match", position, end)
            start = end
            self._match_count += 1
        return self._match_count

    def find_next(self, needle: str, *, backwards: bool = False,
                  case_sensitive: bool = False) -> bool:
        """Move to the next (or previous) match, wrapping around."""
        if not needle:
            return False
        self.text.tag_remove("current_match", "1.0", "end")

        if backwards:
            position = self.text.search(
                needle, "insert", stopindex="1.0", backwards=True,
                nocase=not case_sensitive,
            )
            if not position:
                position = self.text.search(
                    needle, "end", stopindex="1.0", backwards=True,
                    nocase=not case_sensitive,
                )
        else:
            position = self.text.search(
                needle, "insert+1c", stopindex="end", nocase=not case_sensitive
            )
            if not position:
                position = self.text.search(
                    needle, "1.0", stopindex="end", nocase=not case_sensitive
                )
        if not position:
            return False

        end = f"{position}+{len(needle)}c"
        self.text.tag_add("current_match", position, end)
        self.text.mark_set("insert", position)
        self.text.see(position)
        self._redraw_gutter()
        return True

    def replace_current(self, needle: str, replacement: str,
                        *, case_sensitive: bool = False) -> bool:
        """Replace the highlighted match, then advance to the next one."""
        ranges = self.text.tag_ranges("current_match")
        if not ranges:
            return self.find_next(needle, case_sensitive=case_sensitive)
        self.text.delete(ranges[0], ranges[1])
        self.text.insert(ranges[0], replacement)
        self.text.edit_separator()
        self.find_all(needle, case_sensitive=case_sensitive)
        self.find_next(needle, case_sensitive=case_sensitive)
        return True

    def replace_all(self, needle: str, replacement: str,
                    *, case_sensitive: bool = False) -> int:
        """Replace every occurrence; returns how many. One undo step."""
        if not needle:
            return 0
        count = 0
        start = "1.0"
        while True:
            position = self.text.search(
                needle, start, stopindex="end", nocase=not case_sensitive
            )
            if not position:
                break
            end = f"{position}+{len(needle)}c"
            self.text.delete(position, end)
            self.text.insert(position, replacement)
            start = f"{position}+{len(replacement)}c"
            count += 1
        if count:
            self.text.edit_separator()
            self._highlight_now()
        return count

    def clear_matches(self) -> None:
        self.text.tag_remove("match", "1.0", "end")
        self.text.tag_remove("current_match", "1.0", "end")


class _ProxiedText(tk.Text):
    """A Text widget that fires ``<<Change>>`` on any edit or scroll.

    Tk offers no event for "the view moved", which the gutter needs. Renaming the
    widget's own Tcl command and interposing on it is the established way to get
    one, and it catches programmatic edits that a key binding would miss.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original = f"{self._w}_original"
        self.tk.call("rename", self._w, self._original)
        self.tk.createcommand(self._w, self._proxy)

    #: Subcommands that alter content or the view, and so warrant a notification.
    #: ``edit`` is listed only for its mutating forms: ``edit modified`` is a
    #: *query* the status bar makes on every change, and treating it as a change
    #: made the widget notify itself in an endless loop.
    _MUTATING = ("insert", "delete", "replace")
    _EDIT_MUTATIONS = (("edit", "undo"), ("edit", "redo"), ("edit", "reset"))

    def _proxy(self, *args):
        try:
            result = self.tk.call((self._original,) + args)
        except tk.TclError:
            # Tk raises for benign things such as deleting an empty selection.
            return ""

        if args and (
            args[0] in self._MUTATING
            or args[:2] in self._EDIT_MUTATIONS
            or args[:3] == ("mark", "set", "insert")
            or args[:1] in (("yview",), ("xview",))
        ):
            try:
                self.event_generate("<<Change>>", when="tail")
            except tk.TclError:
                pass
        return result


def _split_comment(line: str) -> tuple[str, Optional[int]]:
    """Split a line into its code part and the column where a comment starts.

    Quote-aware, so a ``#`` inside ``"a#b"`` is not treated as a comment — which
    matters for image tags and passwords far more often than it should.
    """
    quote: Optional[str] = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index], index
    return line, None
