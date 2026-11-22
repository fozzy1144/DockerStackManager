"""Import hosts from an OpenSSH config file — the same list VS Code Remote-SSH uses.

Nothing is changed until Import is pressed, and every row states plainly what it
would do. Hosts that are already configured are shown greyed out rather than
hidden, so the dialog answers "is this machine already managed?" as well as
"what can I add?".
"""

from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk

from core.ssh_config import (
    ACTION_ADD,
    ACTION_ATTACH_KEY,
    ACTION_SKIP,
    ImportCandidate,
)
from gui import theme

_ACTION_LABELS = {
    ACTION_ADD: ("Add", theme.SUCCESS),
    ACTION_ATTACH_KEY: ("Attach key", theme.INFO),
    ACTION_SKIP: ("No change", theme.TEXT_3),
}


class ImportHostsDialog(ctk.CTkToplevel):
    """Pick which SSH config hosts to bring in.

    On Import, :attr:`result` holds the chosen candidates; it stays empty if the
    dialog was cancelled. Applying them is the caller's job — this window never
    touches the host list or the keyring.
    """

    def __init__(
        self,
        parent,
        candidates: list[ImportCandidate],
        sources: Optional[list[Path]] = None,
    ):
        super().__init__(parent)
        self.result: list[ImportCandidate] = []
        self._candidates = candidates
        self._checks: list[tuple[ImportCandidate, tk.BooleanVar]] = []

        self.title("Import hosts")
        theme.present(self, parent, (860, 640), minsize=(700, 480))
        self.transient(parent)
        self.grab_set()

        self._build(sources or [])
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self, sources: list[Path]) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 4))

        actionable = sum(1 for c in self._candidates if c.actionable)
        theme.heading(header, "Import from SSH config", role="title").pack(side="left")
        theme.pill(
            header, f"{actionable} of {len(self._candidates)} to import",
            theme.ACCENT_TEXT if actionable else theme.TEXT_3, base=theme.BG, dot=False,
        ).pack(side="left", padx=(12, 0))

        if sources:
            theme.caption(
                self, "Read from  " + ",  ".join(str(path) for path in sources), role="small",
            ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 12))

        panel = theme.card(self)
        panel.grid(row=2, column=0, sticky="nsew", padx=24)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)
        self._rows = theme.scrollable(panel)
        self._rows.grid(row=0, column=0, sticky="nsew", padx=(6, 2), pady=6)
        self._rows.grid_columnconfigure(0, weight=1)

        if self._candidates:
            for candidate in self._candidates:
                self._add_row(candidate)
        else:
            theme.caption(
                self._rows,
                "No hosts found.\n\nVS Code Remote-SSH reads ~/.ssh/config; if "
                "your hosts live elsewhere, point remote.SSH.configFile at it.",
                role="small",
            ).pack(pady=20, padx=12, anchor="w")

        self._build_footer()

    def _add_row(self, candidate: ImportCandidate) -> None:
        host = candidate.host
        enabled = candidate.actionable
        selected = tk.BooleanVar(value=enabled)
        self._checks.append((candidate, selected))

        row = ctk.CTkFrame(
            self._rows, corner_radius=10, border_width=1, border_color=theme.BORDER,
            fg_color=theme.SURFACE_2 if enabled else theme.SURFACE,
        )
        row.pack(fill="x", pady=3, padx=(0, 4))
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(
            row,
            text="",
            variable=selected,
            width=20,
            checkbox_width=18,
            checkbox_height=18,
            state="normal" if enabled else "disabled",
        ).grid(row=0, column=0, padx=(14, 6), pady=10)

        details = ctk.CTkFrame(row, fg_color="transparent")
        details.grid(row=0, column=1, sticky="ew", padx=4, pady=10)

        name = ctk.CTkFrame(details, fg_color="transparent")
        name.pack(fill="x")
        theme.text(name, host.alias, "body_strong",
                   theme.TEXT if enabled else theme.TEXT_3).pack(side="left")
        theme.text(name, host.address, "mono_small", theme.TEXT_3).pack(side="left", padx=(10, 0))

        detail_bits = [candidate.reason]
        if host.identity_file:
            detail_bits.append(f"key {Path(host.identity_file).name}")
        if host.proxy_jump:
            detail_bits.append(f"via {host.proxy_jump}")
        theme.caption(details, "   ·   ".join(b for b in detail_bits if b)).pack(
            fill="x", pady=(2, 0)
        )

        if enabled:
            self.after_idle(lambda: theme.hover_row(row))
        label, color = _ACTION_LABELS.get(candidate.action, ("?", theme.TEXT_3))
        theme.pill(row, label, color, base=row.cget("fg_color"), width=104).grid(
            row=0, column=2, padx=12
        )

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=24, pady=(12, 22))

        theme.button(footer, "Select all", lambda: self._select(True), variant="ghost",
                     size="sm").pack(side="left")
        theme.button(footer, "None", lambda: self._select(False), variant="ghost",
                     size="sm").pack(side="left", padx=(2, 0))

        theme.caption(
            footer,
            "Passwords are never read from the SSH config; key files are referenced, "
            "not copied.",
        ).pack(side="left", padx=14)

        self._btn_import = theme.button(footer, "Import", self._confirm, variant="primary",
                                        width=110)
        self._btn_import.pack(side="right")
        theme.button(footer, "Cancel", self._cancel, variant="ghost").pack(
            side="right", padx=(0, 8)
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────────────

    def _select(self, value: bool) -> None:
        for candidate, variable in self._checks:
            if candidate.actionable:
                variable.set(value)

    def _confirm(self) -> None:
        self.result = [
            candidate
            for candidate, variable in self._checks
            if candidate.actionable and variable.get()
        ]
        self.destroy()

    def _cancel(self) -> None:
        self.result = []
        self.destroy()


def summarize(applied: list[ImportCandidate]) -> str:
    """One-line description of what an import did, for the log."""
    added = sum(1 for c in applied if c.action == ACTION_ADD)
    keyed = sum(1 for c in applied if c.action == ACTION_ATTACH_KEY)
    parts = []
    if added:
        parts.append(f"{added} host(s) added")
    if keyed:
        parts.append(f"{keyed} key path(s) attached")
    return ", ".join(parts) if parts else "nothing to import"
