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

import time
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from core import compose, snippets
from core.ssh_client import SSHClient
from gui import dialogs, theme
from gui.code_editor import CodeEditor
from gui.theme import LEVEL_ACCENTS
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

        self.title(f"{stack.name} — Compose Editor")
        theme.present(self, parent, (1440, 900), minsize=(1100, 660),
                      focus=lambda: self._editor.text.focus_set())

        self._build()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.after(80, self._load)

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._build_toolbar()

        self._snippet_panel = _SnippetBrowser(self, on_insert=self._insert_snippet)
        self._snippet_panel.grid(row=1, column=0, sticky="nsew", padx=(16, 6), pady=(0, 12))

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 16), pady=(0, 12))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._find_bar = _FindBar(right, self._editor_ref, on_close=self._hide_find)
        # Not gridded until Ctrl+F.

        self._editor = CodeEditor(right, on_change=self._on_edit)
        self._editor.grid(row=1, column=0, sticky="nsew")

        self._findings_panel = _FindingsPanel(right, on_select=self._goto_finding)
        self._findings_panel.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        # Status bar: a full-width strip under a hairline, like any editor.
        theme.divider(self).grid(row=2, column=0, columnspan=2, sticky="ew")
        strip = ctk.CTkFrame(self, fg_color=theme.SIDEBAR, corner_radius=0, height=30)
        strip.grid(row=3, column=0, columnspan=2, sticky="ew")
        strip.grid_columnconfigure(0, weight=1)
        self._status = theme.caption(strip, "Loading…", role="caption", color=theme.TEXT_2)
        self._status.grid(row=0, column=0, sticky="w", padx=16, pady=6)
        theme.caption(
            strip, "Ctrl+S save   ·   Ctrl+F find   ·   F5 validate", color=theme.TEXT_3
        ).grid(row=0, column=1, sticky="e", padx=16)

    def _editor_ref(self) -> CodeEditor:
        """Late-bound accessor — the find bar is built before the editor exists."""
        return self._editor

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(16, 12))
        bar.grid_columnconfigure(0, weight=1)

        title = ctk.CTkFrame(bar, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w")
        theme.overline(title, f"{self._stack.name}  ·  compose file").pack(anchor="w")
        path_row = ctk.CTkFrame(title, fg_color="transparent")
        path_row.pack(anchor="w", pady=(2, 0))
        self._path_label = theme.text(path_row, self._stack.compose_file, "mono", theme.TEXT)
        self._path_label.pack(side="left")
        self._dirty_pill = theme.pill(path_row, "", theme.WARNING, base=theme.BG, dot=False)
        self._dirty_pill.pack(side="left", padx=(10, 0))

        buttons = ctk.CTkFrame(bar, fg_color="transparent")
        buttons.grid(row=0, column=1, sticky="e")

        def add(text: str, command, variant: str = "secondary", tip: str = "",
                gap: int = 6) -> ctk.CTkButton:
            button = theme.button(buttons, text, command, variant=variant)
            button.pack(side="left", padx=(0, gap))
            if tip:
                theme.Tooltip(button, tip)
            return button

        self._btn_reload = add("Reload", self._reload, "ghost", "Re-read the file from the host", 2)
        self._btn_find = add("Find", self._show_find, "ghost", "Find and replace  (Ctrl+F)", 10)
        self._btn_diff = add("Diff", self._show_diff, tip="Compare with what is on the host")
        self._btn_validate = add("Validate", self._validate, tip="docker compose config  (F5)")
        self._btn_save = add("Save", self._save, tip="Validate, back up, and write  (Ctrl+S)")
        self._btn_deploy = add(
            "Save & deploy", self._save_and_deploy, "primary",
            "Save, then docker compose up -d", 0,
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

        theme.run_in_thread(task)

    def _on_loaded(self, content: Optional[str], error: str) -> None:
        self._set_busy(False)
        if content is None:
            self._set_status(f"Could not read the file: {error}", compose.ERROR)
            dialogs.showerror(
                "Could not read the file",
                f"{self._stack.compose_file}\n\n{error}",
                parent=self,
            )
            return
        self._original = content
        self._editor.set_text(content)
        self._run_lint()
        self._update_dirty()
        self._set_status("Loaded.")

    def _reload(self) -> None:
        if self._editor.modified and not dialogs.askyesno(
            "Discard your edits?",
            "Re-reading the file from the host replaces everything in the editor.",
            parent=self, confirm="Discard and reload", tone="danger",
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
        if errors and not dialogs.askyesno(
            "Save with errors?",
            f"The checks found {len(errors)} error(s). The first is:\n\n{errors[0]}",
            parent=self, confirm="Save anyway", tone="warning",
        ):
            return

        self._pending_text = text
        self._set_busy(True, "Validating on the host…")

        def task() -> None:
            ok, message = self._ssh.validate_compose(self._stack, text)
            self.after(0, lambda: self._after_validate(ok, message, deploy))

        theme.run_in_thread(task)

    def _after_validate(self, ok: bool, message: str, deploy: bool) -> None:
        self._set_busy(False)
        if not ok and not dialogs.askyesno(
            "Docker rejected the file",
            f"docker compose config failed on the host. Saving anyway may leave the "
            f"stack unable to start.\n\n{message}",
            parent=self, confirm="Save anyway", tone="warning",
        ):
            self._set_status(f"Not saved — {_first_line(message)}", compose.ERROR, sticky=True)
            return

        self._set_busy(True, "Writing to host…")
        text = self._pending_text

        def task() -> None:
            saved, detail = self._ssh.write_text(self._stack.compose_file, text)
            self.after(0, lambda: self._after_save(saved, detail, deploy, text))

        theme.run_in_thread(task)

    def _after_save(self, saved: bool, detail: str, deploy: bool, text: str) -> None:
        self._set_busy(False)
        if not saved:
            self._set_status(f"Save failed: {detail}", compose.ERROR, sticky=True)
            self._log(f"{self._stack.name}: save failed — {detail}", "error")
            dialogs.showerror("Save failed", detail, parent=self)
            return

        self._original = text
        self._editor.mark_clean()
        self._update_dirty()
        backup_note = f" Backup: {detail}" if detail else ""
        self._set_status(f"Saved.{backup_note}", "saved", sticky=True)
        self._log(f"{self._stack.name}: compose file saved.{backup_note}", "success")
        if not deploy:
            dialogs.toast(self, "Saved to host", detail=detail and f"Backup: {detail}")

        if deploy:
            self._deploy()

    def _deploy(self) -> None:
        self._set_busy(True, "Deploying…")
        self._log(f"{self._stack.name}: deploying edited stack…")

        def task() -> None:
            ok = self._ssh.compose_action(self._stack, "up", self._log)
            self.after(0, lambda: self._after_deploy(ok))

        theme.run_in_thread(task)

    def _after_deploy(self, ok: bool) -> None:
        self._set_busy(False)
        self._set_status(
            "Deployed." if ok else "Deploy failed — see the main window log.",
            "saved" if ok else compose.ERROR,
            sticky=True,
        )
        dialogs.toast(self, "Deployed" if ok else "Deploy failed",
                      "success" if ok else "error",
                      detail="" if ok else "See the main window's activity log.")
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

        theme.run_in_thread(task)

    def _on_validated(self, ok: bool, message: str) -> None:
        self._set_busy(False)
        if ok:
            self._set_status(f"Docker accepted the file. {compose.summarize(self._findings)}", "saved", sticky=True)
        else:
            self._set_status(f"Docker rejected it: {_first_line(message)}", compose.ERROR, sticky=True)
            dialogs.showwarning(
                "Docker rejected the file",
                f"docker compose config reported a problem.\n\n{message}",
                parent=self,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Linting
    # ──────────────────────────────────────────────────────────────────────────

    def _on_edit(self) -> None:
        self._update_dirty()
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
            dialogs.toast(self, "No changes", "info",
                          detail="The editor matches the file on the host.")
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

    def _update_dirty(self) -> None:
        """The "Unsaved" chip beside the path, and an asterisk in the title."""
        dirty = self._editor.modified
        if dirty == getattr(self, "_dirty_shown", None):
            return
        self._dirty_shown = dirty
        theme.set_pill(self._dirty_pill, "Unsaved" if dirty else "", theme.WARNING,
                       base=theme.BG, dot=False)
        prefix = "● " if dirty else ""
        self.title(f"{prefix}{self._stack.name} — Compose Editor")

    def _update_status_position(self) -> None:
        """Show cursor position and the lint summary — the idle state of the bar."""
        if self._busy or time.monotonic() < self._sticky_until:
            return
        line, column = self._editor.cursor_position()
        self._set_status(
            f"Ln {line}, Col {column}     {self._editor.line_count()} lines"
            f"     {compose.summarize(self._findings)}"
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
            text=message, text_color=LEVEL_ACCENTS.get(level, theme.TEXT_2)
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Closing
    # ──────────────────────────────────────────────────────────────────────────

    def _close(self) -> None:
        if self._busy:
            if not dialogs.askyesno(
                "Still working",
                "An operation is still running. Closing now will not stop it on the host.",
                parent=self, confirm="Close anyway", tone="warning",
            ):
                return
        elif self._editor.modified and not dialogs.askyesno(
            "Close without saving?",
            "Your edits to this compose file have not been saved to the host.",
            parent=self, confirm="Discard edits", tone="danger",
        ):
            return
        self.destroy()


class _SnippetBrowser(ctk.CTkFrame):
    """Category list of example configurations, with their customization notes."""

    def __init__(self, parent, on_insert: Callable[[snippets.Snippet], None]):
        super().__init__(parent, width=340, fg_color=theme.SURFACE, corner_radius=12,
                         border_width=1, border_color=theme.BORDER)
        self._on_insert = on_insert
        self._grouped = snippets.by_category()
        self._selected: Optional[snippets.Snippet] = None
        self._buttons: dict[str, ctk.CTkButton] = {}

        self.grid_propagate(False)
        self.grid_rowconfigure(2, weight=2)
        self.grid_rowconfigure(4, weight=3)
        self.grid_columnconfigure(0, weight=1)

        theme.heading(self, "Examples", role="heading").grid(
            row=0, column=0, sticky="w", padx=16, pady=(14, 8)
        )

        self._category = ctk.CTkOptionMenu(
            self,
            values=list(self._grouped),
            command=self._on_category_changed,
            height=32,
            font=theme.font("small_strong"),
            dropdown_font=theme.font("small"),
        )
        self._category.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._list = theme.scrollable(self, height=120)
        self._list.grid(row=2, column=0, sticky="nsew", padx=(8, 4))
        self._list.grid_columnconfigure(0, weight=1)

        self._insert_button = theme.button(
            self, "Insert at cursor", self._insert, variant="primary", state="disabled"
        )
        self._insert_button.grid(row=3, column=0, sticky="ew", padx=12, pady=10)

        self._details = ctk.CTkTextbox(
            self, state="disabled", wrap="word", font=(theme.UI_FAMILY, 12),
            fg_color=theme.INSET, border_spacing=10, height=120,
        )
        self._details.grid(row=4, column=0, sticky="nsew", padx=6, pady=(0, 6))
        theme.style_textbox(self._details)
        self._details.tag_config("summary", foreground=theme.TEXT, spacing3=8)
        # CTkTextbox refuses per-tag fonts; the underlying Tk widget does not.
        self._details._textbox.tag_config("code", foreground="#9CDCFE",
                                          font=theme.scaled_mono(self, 11), lmargin1=8, lmargin2=8)
        self._details.tag_config("notes", foreground=theme.TEXT_2, spacing1=2)
        self._details.tag_config("link", foreground=theme.ACCENT_TEXT)
        self._details.tag_config("rule", foreground=theme.BORDER_STRONG)

        first = next(iter(self._grouped), "")
        if first:
            self._on_category_changed(first)
        self._show_hint()

    def set_services(self, _services: list[str]) -> None:
        """Hook for future service-aware suggestions; currently informational."""

    def _show_hint(self) -> None:
        self._write([("Pick an example to see what it does, the YAML it inserts, "
                      "and what to change before using it.", "notes")])

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
                corner_radius=7,
                fg_color="transparent",
                text_color=theme.TEXT_2,
                hover_color=theme.SURFACE_3,
                font=theme.font("small"),
                command=lambda s=snippet: self._select(s),
            )
            button.pack(fill="x", pady=1, padx=(0, 4))
            self._buttons[snippet.title] = button

    def _select(self, snippet: snippets.Snippet) -> None:
        self._selected = snippet
        self._insert_button.configure(state="normal")
        selected_bg = theme.tint(theme.ACCENT, 0.20, theme.SURFACE)
        for title, button in self._buttons.items():
            active = title == snippet.title
            button.configure(
                fg_color=selected_bg if active else "transparent",
                text_color=theme.TEXT if active else theme.TEXT_2,
                hover_color=selected_bg if active else theme.SURFACE_3,
            )

        parts = [
            (snippet.summary + "\n", "summary"),
            (snippet.body.rstrip() + "\n", "code"),
            ("\n" + "─" * 36 + "\n\n", "rule"),
            (snippet.details.strip() + "\n", "notes"),
        ]
        if snippet.docs_url:
            parts.append((f"\nReference: {snippet.docs_url}", "link"))
        self._write(parts)

    def _write(self, parts: list[tuple[str, str]]) -> None:
        self._details.configure(state="normal")
        self._details.delete("1.0", "end")
        for text, tag in parts:
            self._details.insert("end", text, tag)
        self._details.configure(state="disabled")

    def _insert(self) -> None:
        if self._selected is not None:
            self._on_insert(self._selected)


class _FindingsPanel(ctk.CTkFrame):
    """Lint results, clickable to jump to the offending line."""

    _MAX_ROWS = 60

    def __init__(self, parent, on_select: Callable[[compose.Finding], None]):
        super().__init__(parent, height=168, fg_color=theme.SURFACE, corner_radius=12,
                         border_width=1, border_color=theme.BORDER)
        self._on_select = on_select

        self.grid_propagate(False)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))
        theme.heading(head, "Checks", role="heading").pack(side="left")
        self._header = theme.pill(head, "", theme.TEXT_3, base=theme.SURFACE, dot=False)
        self._header.pack(side="left", padx=(10, 0))

        self._rows = theme.scrollable(self, height=60)
        self._rows.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 2))
        self._rows.grid_columnconfigure(0, weight=1)

        self._hint = theme.caption(self, "", role="small", color=theme.TEXT_2,
                                   wraplength=900)
        self._hint.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))

    def show(self, findings: list[compose.Finding]) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._hint.configure(text="")

        if not findings:
            theme.set_pill(self._header, "No issues", theme.SUCCESS, base=theme.SURFACE)
            theme.caption(
                self._rows, "✓  Nothing to report.", color=theme.SUCCESS, role="small"
            ).pack(fill="x", padx=8, pady=4)
            return

        worst = (compose.ERROR if any(f.is_error for f in findings)
                 else compose.WARNING if any(f.level == compose.WARNING for f in findings)
                 else compose.INFO)
        theme.set_pill(self._header, compose.summarize(findings),
                       LEVEL_ACCENTS.get(worst, theme.TEXT_2), base=theme.SURFACE, dot=False)
        for finding in findings[: self._MAX_ROWS]:
            self._add_row(finding)
        if len(findings) > self._MAX_ROWS:
            theme.caption(
                self._rows, f"…and {len(findings) - self._MAX_ROWS} more.", role="small"
            ).pack(fill="x", padx=8)

    def _add_row(self, finding: compose.Finding) -> None:
        location = f"line {finding.line}" if finding.line else "file"
        color = LEVEL_ACCENTS.get(finding.level, theme.TEXT_2)
        row = ctk.CTkButton(
            self._rows,
            text=f"{_LEVEL_ICONS.get(finding.level, '•')}   {location:<9}  {finding.message}",
            anchor="w",
            height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.SURFACE_3,
            text_color=color,
            font=theme.font("small"),
            command=lambda: self._select(finding),
        )
        row.pack(fill="x", pady=0, padx=(0, 4))

    def _select(self, finding: compose.Finding) -> None:
        self._hint.configure(text=finding.hint or finding.message)
        self._on_select(finding)


class _FindBar(ctk.CTkFrame):
    """Find and replace strip above the editor."""

    def __init__(self, parent, editor: Callable[[], CodeEditor], on_close: Callable[[], None]):
        super().__init__(parent, fg_color=theme.SURFACE, corner_radius=10,
                         border_width=1, border_color=theme.BORDER_STRONG)
        self._editor = editor
        self._case = tk.BooleanVar(value=False)

        self._find = ctk.CTkEntry(self, placeholder_text="Find", width=220, height=28,
                                  font=theme.font("small"))
        self._find.pack(side="left", padx=(8, 4), pady=8)
        self._find.bind("<Return>", lambda _e: self._next())
        self._find.bind("<KeyRelease>", lambda _e: self._refresh_matches())

        self._replace = ctk.CTkEntry(
            self, placeholder_text="Replace with", width=200, height=28,
            font=theme.font("small"),
        )
        self._replace.pack(side="left", padx=4, pady=8)

        for text, command, variant, tip in (
            ("↑", self._previous, "ghost", "Previous match"),
            ("↓", self._next, "ghost", "Next match  (Enter)"),
            ("Replace", self._replace_one, "secondary", ""),
            ("All", self._replace_all, "secondary", "Replace all"),
        ):
            button = theme.button(self, text, command, variant=variant, size="sm")
            button.pack(side="left", padx=2, pady=8)
            if tip:
                theme.Tooltip(button, tip)

        ctk.CTkCheckBox(
            self, text="Match case", variable=self._case, width=44, font=theme.font("small"),
            checkbox_width=16, checkbox_height=16, command=self._refresh_matches,
        ).pack(side="left", padx=(10, 6))

        self._count = theme.caption(self, "", role="small", width=90)
        self._count.pack(side="left", padx=4)

        theme.icon_button(self, "✕", on_close, size=26).pack(side="right", padx=8)

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
        theme.present(self, parent, (1000, 640))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        added = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 10))
        theme.heading(head, "Changes to be saved", role="title").pack(side="left")
        theme.pill(head, f"+{added}", theme.SUCCESS, base=theme.BG, dot=False).pack(
            side="left", padx=(12, 4))
        theme.pill(head, f"−{removed}", theme.DANGER, base=theme.BG, dot=False).pack(side="left")
        theme.button(head, "Close", self.destroy, variant="secondary", size="sm").pack(side="right")

        text = ctk.CTkTextbox(self, font=theme.mono_tuple(12), wrap="none",
                              fg_color=theme.INSET, border_spacing=8, corner_radius=10,
                              border_width=1, border_color=theme.BORDER)
        text.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        theme.style_textbox(text)

        text.tag_config("added", foreground="#7EE787",
                        background=theme.tint(theme.SUCCESS, 0.10, theme.INSET))
        text.tag_config("removed", foreground="#FF8E8A",
                        background=theme.tint(theme.DANGER, 0.10, theme.INSET))
        text.tag_config("header", foreground=theme.INFO)
        text.tag_config("context", foreground=theme.TEXT_2)

        for line in patch.splitlines(keepends=True):
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                tag = "header"
            elif line.startswith("+"):
                tag = "added"
            elif line.startswith("-"):
                tag = "removed"
            else:
                tag = "context"
            text.insert("end", line, tag)
        text.configure(state="disabled")

        self.bind("<Escape>", lambda _e: self.destroy())


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text.strip()
