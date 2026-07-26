"""The Compose file editor window.

Fetches a stack's compose file over SSH, edits it locally with live linting, and
writes it back with a timestamped backup. Two safety rails sit in front of the
save, because this window can take a running stack down:

1. :mod:`core.compose` lints the text as you type — instant, offline, and aware
   of the mistakes that only show up after a restart.
2. ``docker compose config`` on the host validates for real, resolving ``.env``
   interpolation and override files exactly as the deploy will.

Neither one blocks a determined save; both make you acknowledge what you are
doing first.
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk

from core import compose, snippets
from core.ssh_client import SSHClient
from gui.code_editor import CodeEditor
from gui.theme import (
    ACCENT_PURPLE,
    ACCENT_PURPLE_HOVER,
    LEVEL_ACCENTS,
    SELECTED,
)
from models.host import DockerStack

_LEVEL_ICONS = {compose.ERROR: "✕", compose.WARNING: "▲", compose.INFO: "i"}

_STICKY_SECONDS = 8.0
"""How long an outcome message holds the status bar against routine updates."""


class ComposeEditor(ctk.CTkToplevel):
    """Edit one stack's compose file.

    ``on_deployed`` is called after a successful "Save & Deploy" so the main
    window can refresh the stack's status.
    """

    def __init__(
        self,
        parent,
        ssh: SSHClient,
        stack: DockerStack,
        log: Callable[..., None],
        on_deployed: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self._ssh = ssh
        self._stack = stack
        self._log = log
        self._on_deployed = on_deployed

        self._original = ""
        self._pending_text = ""
        self._busy = False
        self._findings: list[compose.Finding] = []
        self._lint_job: Optional[str] = None
        self._linted_text: Optional[str] = None
        """Text as of the last lint pass; an unchanged buffer is not re-linted."""

        self._last_status: tuple[str, str] = ("", "")
        self._sticky_until = 0.0

        self.title(f"Compose Editor — {stack.name}")
        self.geometry("1500x900")
        self.minsize(1100, 640)

        self._build()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.after(80, self._load)
        self.after(120, self._focus_window)

    def _focus_window(self) -> None:
        self.lift()
        self.focus_force()
        self._editor.text.focus_set()

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._build_toolbar()

        self._snippet_panel = _SnippetBrowser(self, on_insert=self._insert_snippet)
        self._snippet_panel.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=4)

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=4)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._find_bar = _FindBar(right, self._editor_ref, on_close=self._hide_find)
        # Not gridded until Ctrl+F.

        self._editor = CodeEditor(right, on_change=self._on_edit)
        self._editor.grid(row=1, column=0, sticky="nsew")

        self._findings_panel = _FindingsPanel(right, on_select=self._goto_finding)
        self._findings_panel.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self._status = ctk.CTkLabel(
            self, text="Loading…", anchor="w", font=ctk.CTkFont(size=11)
        )
        self._status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 6))

    def _editor_ref(self) -> CodeEditor:
        """Late-bound accessor — the find bar is built before the editor exists."""
        return self._editor

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 0))

        self._path_label = ctk.CTkLabel(
            bar,
            text=self._stack.compose_file,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self._path_label.pack(side="left", padx=(4, 12))

        buttons = ctk.CTkFrame(bar, fg_color="transparent")
        buttons.pack(side="right")

        def add(text: str, command, width: int = 92, **kwargs) -> ctk.CTkButton:
            button = ctk.CTkButton(
                buttons, text=text, width=width, height=28, command=command, **kwargs
            )
            button.pack(side="left", padx=3)
            return button

        self._btn_reload = add("Reload", self._reload, 74)
        self._btn_find = add("Find", self._show_find, 62)
        self._btn_diff = add("Diff", self._show_diff, 62)
        self._btn_validate = add("Validate", self._validate, 84)
        self._btn_save = add("Save", self._save, 74)
        self._btn_deploy = add(
            "Save & Deploy",
            self._save_and_deploy,
            118,
            fg_color=ACCENT_PURPLE,
            hover_color=ACCENT_PURPLE_HOVER,
        )

    def _bind_shortcuts(self) -> None:
        for sequence, handler in (
            ("<Control-s>", lambda _e: self._save()),
            ("<Control-S>", lambda _e: self._save()),
            ("<Control-f>", lambda _e: self._show_find()),
            ("<Control-F>", lambda _e: self._show_find()),
            ("<F5>", lambda _e: self._validate()),
            ("<Escape>", lambda _e: self._hide_find()),
        ):
            self.bind(sequence, handler)

    # ──────────────────────────────────────────────────────────────────────────
    # Loading and saving
    # ──────────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._set_busy(True, "Reading file from host…")

        def task() -> None:
            content, error = self._ssh.read_text(self._stack.compose_file)
            self.after(0, lambda: self._on_loaded(content, error))

        _in_thread(task)

    def _on_loaded(self, content: Optional[str], error: str) -> None:
        self._set_busy(False)
        if content is None:
            self._set_status(f"Could not read the file: {error}", compose.ERROR)
            messagebox.showerror(
                "Could not read file",
                f"{self._stack.compose_file}\n\n{error}",
                parent=self,
            )
            return
        self._original = content
        self._editor.set_text(content)
        self._run_lint()
        self._set_status("Loaded.")

    def _reload(self) -> None:
        if self._editor.modified and not messagebox.askyesno(
            "Discard changes?",
            "Re-reading the file from the host will discard your edits.\n\nContinue?",
            parent=self,
        ):
            return
        self._load()

    def _save(self) -> None:
        self._begin_save(deploy=False)

    def _save_and_deploy(self) -> None:
        self._begin_save(deploy=True)

    def _begin_save(self, *, deploy: bool) -> None:
        """Start the save chain: local lint → remote validate → write → deploy."""
        if self._busy:
            return
        text = self._editor.get_text()
        if text == self._original and not deploy:
            self._set_status("No changes to save.")
            return

        errors = [f for f in self._findings if f.is_error]
        if errors and not messagebox.askyesno(
            "Save with errors?",
            f"The file has {len(errors)} error(s), starting with:\n\n"
            f"{errors[0]}\n\nSave it anyway?",
            parent=self,
        ):
            return

        self._pending_text = text
        self._set_busy(True, "Validating on the host…")

        def task() -> None:
            ok, message = self._ssh.validate_compose(self._stack, text)
            self.after(0, lambda: self._after_validate(ok, message, deploy))

        _in_thread(task)

    def _after_validate(self, ok: bool, message: str, deploy: bool) -> None:
        self._set_busy(False)
        if not ok and not messagebox.askyesno(
            "Docker rejected the file",
            f"{message}\n\nSave it anyway?",
            parent=self,
        ):
            self._set_status(f"Not saved — {_first_line(message)}", compose.ERROR, sticky=True)
            return

        self._set_busy(True, "Writing to host…")
        text = self._pending_text

        def task() -> None:
            saved, detail = self._ssh.write_text(self._stack.compose_file, text)
            self.after(0, lambda: self._after_save(saved, detail, deploy, text))

        _in_thread(task)

    def _after_save(self, saved: bool, detail: str, deploy: bool, text: str) -> None:
        self._set_busy(False)
        if not saved:
            self._set_status(f"Save failed: {detail}", compose.ERROR, sticky=True)
            self._log(f"{self._stack.name}: save failed — {detail}", "error")
            messagebox.showerror("Save failed", detail, parent=self)
            return

        self._original = text
        self._editor.mark_clean()
        backup_note = f" Backup: {detail}" if detail else ""
        self._set_status(f"Saved.{backup_note}", "saved", sticky=True)
        self._log(f"{self._stack.name}: compose file saved.{backup_note}", "success")

        if deploy:
            self._deploy()

    def _deploy(self) -> None:
        self._set_busy(True, "Deploying…")
        self._log(f"{self._stack.name}: deploying edited stack…")

        def task() -> None:
            ok = self._ssh.compose_action(self._stack, "up", self._log)
            self.after(0, lambda: self._after_deploy(ok))

        _in_thread(task)

    def _after_deploy(self, ok: bool) -> None:
        self._set_busy(False)
        self._set_status(
            "Deployed." if ok else "Deploy failed — see the main window log.",
            "saved" if ok else compose.ERROR,
            sticky=True,
        )
        if ok and self._on_deployed is not None:
            self._on_deployed()

    def _validate(self) -> None:
        """Run the host-side validator on demand."""
        if self._busy:
            return
        text = self._editor.get_text()
        self._set_busy(True, "Validating on the host…")

        def task() -> None:
            ok, message = self._ssh.validate_compose(self._stack, text)
            self.after(0, lambda: self._on_validated(ok, message))

        _in_thread(task)

    def _on_validated(self, ok: bool, message: str) -> None:
        self._set_busy(False)
        if ok:
            self._set_status(f"Docker accepted the file. {compose.summarize(self._findings)}", "saved", sticky=True)
        else:
            self._set_status(f"Docker rejected it: {_first_line(message)}", compose.ERROR, sticky=True)
            messagebox.showwarning("Validation failed", message, parent=self)

    # ──────────────────────────────────────────────────────────────────────────
    # Linting
    # ──────────────────────────────────────────────────────────────────────────

    def _on_edit(self) -> None:
        self._update_status_position()
        if self._lint_job is not None:
            try:
                self.after_cancel(self._lint_job)
            except tk.TclError:
                pass
        try:
            self._lint_job = self.after(300, self._run_lint)
        except tk.TclError:
            self._lint_job = None

    def _run_lint(self) -> None:
        """Re-check the buffer, unless it is the one already checked.

        Cursor movement reaches here too — it fires the editor's change event so
        the gutter can follow — and a re-lint then means parsing the document and
        rebuilding the findings list to produce exactly the same answer.
        """
        self._lint_job = None
        text = self._editor.get_text()
        if text == self._linted_text:
            return
        self._linted_text = text

        self._findings = compose.lint(text)
        self._findings_panel.show(self._findings)
        self._editor.highlight_error_lines(
            [f.line for f in self._findings if f.is_error and f.line]
        )
        self._snippet_panel.set_services(compose.service_names(text))
        self._update_status_position()

    def _goto_finding(self, finding: compose.Finding) -> None:
        if finding.line:
            self._editor.goto_line(finding.line)

    # ──────────────────────────────────────────────────────────────────────────
    # Snippets, find, diff
    # ──────────────────────────────────────────────────────────────────────────

    def _insert_snippet(self, snippet: snippets.Snippet) -> None:
        indent = self._snippet_indent(snippet)
        self._editor.insert_block(snippets.reindent(snippet.body, indent))
        self._run_lint()
        self._set_status(f"Inserted '{snippet.title}' at indent {indent}.")

    def _snippet_indent(self, snippet: snippets.Snippet) -> int:
        """Where the snippet should sit, based on its kind and the cursor.

        A root-level block always goes to column 0; otherwise the cursor's own
        indentation wins, falling back to the conventional depth for that kind.
        """
        if snippet.kind == "root":
            return 0
        current = self._editor.current_indent()
        if current:
            return current
        return 2 if snippet.kind == "service" else 4

    def _show_find(self) -> None:
        self._find_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self._find_bar.focus_entry()

    def _hide_find(self) -> None:
        self._find_bar.grid_remove()
        self._editor.clear_matches()
        self._editor.text.focus_set()

    def _show_diff(self) -> None:
        patch = compose.diff(
            self._original, self._editor.get_text(), self._stack.compose_file
        )
        if not patch:
            messagebox.showinfo(
                "No changes", "The file matches what is on the host.", parent=self
            )
            return
        _DiffWindow(self, patch)

    # ──────────────────────────────────────────────────────────────────────────
    # Status
    # ──────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._btn_reload,
            self._btn_validate,
            self._btn_save,
            self._btn_deploy,
            self._btn_diff,
        ):
            button.configure(state=state)
        self.configure(cursor="watch" if busy else "")
        if message:
            self._set_status(message)

    def _update_status_position(self) -> None:
        """Show cursor position and the lint summary — the idle state of the bar."""
        if self._busy or time.monotonic() < self._sticky_until:
            return
        line, column = self._editor.cursor_position()
        marker = " • modified" if self._editor.modified else ""
        self._set_status(
            f"Ln {line}, Col {column}   |   {self._editor.line_count()} lines"
            f"   |   {compose.summarize(self._findings)}{marker}"
        )

    def _set_status(self, message: str, level: str = "", sticky: bool = False) -> None:
        """Update the status bar.

        ``sticky`` holds the message for a few seconds so an outcome — saved,
        deployed, rejected — is not immediately overwritten by the routine
        position readout the next lint pass triggers.
        """
        if sticky:
            self._sticky_until = time.monotonic() + _STICKY_SECONDS
        # Reconfiguring a CTkLabel redraws it, and this runs on every keystroke,
        # so skip identical updates.
        if (message, level) == self._last_status:
            return
        self._last_status = (message, level)
        self._status.configure(
            text=message, text_color=LEVEL_ACCENTS.get(level, "gray70")
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Closing
    # ──────────────────────────────────────────────────────────────────────────

    def _close(self) -> None:
        if self._busy:
            if not messagebox.askyesno(
                "Still working",
                "An operation is still running. Close anyway?",
                parent=self,
            ):
                return
        elif self._editor.modified and not messagebox.askyesno(
            "Discard changes?",
            "The file has unsaved changes.\n\nClose without saving?",
            parent=self,
        ):
            return
        self.destroy()


class _SnippetBrowser(ctk.CTkFrame):
    """Category list of example configurations, with their customization notes."""

    def __init__(self, parent, on_insert: Callable[[snippets.Snippet], None]):
        super().__init__(parent, width=330)
        self._on_insert = on_insert
        self._grouped = snippets.by_category()
        self._selected: Optional[snippets.Snippet] = None
        self._buttons: dict[str, ctk.CTkButton] = {}

        self.grid_propagate(False)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(4, weight=2)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Examples", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))

        self._category = ctk.CTkOptionMenu(
            self,
            values=list(self._grouped),
            command=self._on_category_changed,
        )
        self._category.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        self._list = ctk.CTkScrollableFrame(self, label_text="")
        self._list.grid(row=2, column=0, sticky="nsew", padx=10)
        self._list.grid_columnconfigure(0, weight=1)

        self._insert_button = ctk.CTkButton(
            self, text="Insert at cursor", command=self._insert, state="disabled"
        )
        self._insert_button.grid(row=3, column=0, sticky="ew", padx=10, pady=6)

        self._details = ctk.CTkTextbox(
            self, state="disabled", wrap="word", font=("Segoe UI", 11)
        )
        self._details.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 8))

        first = next(iter(self._grouped), "")
        if first:
            self._on_category_changed(first)

    def set_services(self, _services: list[str]) -> None:
        """Hook for future service-aware suggestions; currently informational."""

    def _on_category_changed(self, category: str) -> None:
        for widget in self._list.winfo_children():
            widget.destroy()
        self._buttons.clear()

        for snippet in self._grouped.get(category, []):
            button = ctk.CTkButton(
                self._list,
                text=snippet.title,
                anchor="w",
                height=30,
                fg_color="transparent",
                text_color="gray85",
                hover_color="#2B5278",
                command=lambda s=snippet: self._select(s),
            )
            button.pack(fill="x", pady=1)
            self._buttons[snippet.title] = button

    def _select(self, snippet: snippets.Snippet) -> None:
        self._selected = snippet
        self._insert_button.configure(state="normal")
        for title, button in self._buttons.items():
            button.configure(
                fg_color=SELECTED if title == snippet.title else "transparent"
            )

        body = [
            snippet.summary,
            "",
            snippet.body.rstrip(),
            "",
            "─" * 44,
            "",
            snippet.details.strip(),
        ]
        if snippet.docs_url:
            body += ["", f"Reference: {snippet.docs_url}"]

        self._details.configure(state="normal")
        self._details.delete("1.0", "end")
        self._details.insert("1.0", "\n".join(body))
        self._details.configure(state="disabled")

    def _insert(self) -> None:
        if self._selected is not None:
            self._on_insert(self._selected)


class _FindingsPanel(ctk.CTkFrame):
    """Lint results, clickable to jump to the offending line."""

    _MAX_ROWS = 60

    def __init__(self, parent, on_select: Callable[[compose.Finding], None]):
        super().__init__(parent, height=150)
        self._on_select = on_select

        self.grid_propagate(False)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._header = ctk.CTkLabel(
            self, text="Checks", font=ctk.CTkFont(size=12, weight="bold"), anchor="w"
        )
        self._header.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))

        self._rows = ctk.CTkScrollableFrame(self, label_text="")
        self._rows.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 2))
        self._rows.grid_columnconfigure(0, weight=1)

        self._hint = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self._hint.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))

    def show(self, findings: list[compose.Finding]) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._hint.configure(text="")

        if not findings:
            self._header.configure(text="Checks — no issues found")
            ctk.CTkLabel(
                self._rows, text="Nothing to report.", text_color="#4CAF50", anchor="w"
            ).pack(fill="x", padx=4, pady=2)
            return

        self._header.configure(text=f"Checks — {compose.summarize(findings)}")
        for finding in findings[: self._MAX_ROWS]:
            self._add_row(finding)
        if len(findings) > self._MAX_ROWS:
            ctk.CTkLabel(
                self._rows,
                text=f"…and {len(findings) - self._MAX_ROWS} more.",
                text_color="gray60",
                anchor="w",
            ).pack(fill="x", padx=4)

    def _add_row(self, finding: compose.Finding) -> None:
        location = f"line {finding.line}" if finding.line else "file"
        row = ctk.CTkButton(
            self._rows,
            text=f"{_LEVEL_ICONS.get(finding.level, '•')}  {location}  —  {finding.message}",
            anchor="w",
            height=24,
            fg_color="transparent",
            hover_color="#2B5278",
            text_color=LEVEL_ACCENTS.get(finding.level, "gray80"),
            font=ctk.CTkFont(size=11),
            command=lambda: self._select(finding),
        )
        row.pack(fill="x", pady=1)

    def _select(self, finding: compose.Finding) -> None:
        self._hint.configure(text=finding.hint or finding.message)
        self._on_select(finding)


class _FindBar(ctk.CTkFrame):
    """Find and replace strip above the editor."""

    def __init__(self, parent, editor: Callable[[], CodeEditor], on_close: Callable[[], None]):
        super().__init__(parent, fg_color="#2A2A2A", corner_radius=6)
        self._editor = editor
        self._case = tk.BooleanVar(value=False)

        self._find = ctk.CTkEntry(self, placeholder_text="Find", width=220, height=26)
        self._find.pack(side="left", padx=(8, 4), pady=6)
        self._find.bind("<Return>", lambda _e: self._next())
        self._find.bind("<KeyRelease>", lambda _e: self._refresh_matches())

        self._replace = ctk.CTkEntry(
            self, placeholder_text="Replace with", width=200, height=26
        )
        self._replace.pack(side="left", padx=4, pady=6)

        for text, command, width in (
            ("▲", self._previous, 34),
            ("▼", self._next, 34),
            ("Replace", self._replace_one, 78),
            ("All", self._replace_all, 50),
        ):
            ctk.CTkButton(
                self, text=text, width=width, height=26, command=command
            ).pack(side="left", padx=2, pady=6)

        ctk.CTkCheckBox(
            self, text="Aa", variable=self._case, width=44,
            command=self._refresh_matches,
        ).pack(side="left", padx=6)

        self._count = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11), text_color="gray70", width=90
        )
        self._count.pack(side="left", padx=4)

        ctk.CTkButton(self, text="✕", width=28, height=26, command=on_close).pack(
            side="right", padx=8
        )

    def focus_entry(self) -> None:
        self._find.focus_set()
        self._refresh_matches()

    def _needle(self) -> str:
        return self._find.get()

    def _refresh_matches(self) -> None:
        count = self._editor().find_all(
            self._needle(), case_sensitive=self._case.get()
        )
        self._count.configure(text=f"{count} match{'' if count == 1 else 'es'}")

    def _next(self) -> None:
        self._editor().find_next(self._needle(), case_sensitive=self._case.get())

    def _previous(self) -> None:
        self._editor().find_next(
            self._needle(), backwards=True, case_sensitive=self._case.get()
        )

    def _replace_one(self) -> None:
        self._editor().replace_current(
            self._needle(), self._replace.get(), case_sensitive=self._case.get()
        )
        self._refresh_matches()

    def _replace_all(self) -> None:
        count = self._editor().replace_all(
            self._needle(), self._replace.get(), case_sensitive=self._case.get()
        )
        self._count.configure(text=f"{count} replaced")


class _DiffWindow(ctk.CTkToplevel):
    """Unified diff of the edits against what is on the host."""

    def __init__(self, parent, patch: str):
        super().__init__(parent)
        self.title("Changes to be saved")
        self.geometry("980x620")

        text = ctk.CTkTextbox(self, font=("Consolas", 12), wrap="none")
        text.pack(fill="both", expand=True, padx=10, pady=10)

        text.tag_config("added", foreground="#7EE787")
        text.tag_config("removed", foreground="#FF7B72")
        text.tag_config("header", foreground="#79C0FF")

        for line in patch.splitlines(keepends=True):
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                tag = "header"
            elif line.startswith("+"):
                tag = "added"
            elif line.startswith("-"):
                tag = "removed"
            else:
                tag = ""
            text.insert("end", line, tag)
        text.configure(state="disabled")

        ctk.CTkButton(self, text="Close", command=self.destroy, width=100).pack(
            pady=(0, 10)
        )
        self.after(80, self.lift)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text.strip()


def _in_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()
