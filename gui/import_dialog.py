"""Import hosts from an OpenSSH config file — the same list VS Code Remote-SSH uses.

Nothing is changed until Import is pressed, and every row states plainly what it
would do. Hosts that are already configured are shown greyed out rather than
hidden, so the dialog answers "is this machine already managed?" as well as
"what can I add?".
"""

from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from core.ssh_config import (
    ACTION_ADD,
    ACTION_ATTACH_KEY,
    ACTION_SKIP,
    ImportCandidate,
)
from gui.theme import LEVEL_ACCENTS, MUTED, SELECTED

_ACTION_LABELS = {
    ACTION_ADD: ("add", LEVEL_ACCENTS["success"]),
    ACTION_ATTACH_KEY: ("attach key", LEVEL_ACCENTS["info"]),
    ACTION_SKIP: ("no change", MUTED),
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

        self.title("Import hosts from SSH config")
        self.geometry("880x680")
        self.minsize(700, 480)
        self.grab_set()

        self._build(sources or [])
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.after(80, self._focus_window)

    def _focus_window(self) -> None:
        self.lift()
        self.focus_force()

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self, sources: list[Path]) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 2))

        actionable = sum(1 for c in self._candidates if c.actionable)
        ctk.CTkLabel(
            header,
            text=f"{len(self._candidates)} host(s) found, {actionable} to import",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(side="left")

        if sources:
            ctk.CTkLabel(
                self,
                text="Read from: " + ", ".join(str(path) for path in sources),
                font=ctk.CTkFont(size=11),
                text_color=MUTED,
                anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 6))

        self._rows = ctk.CTkScrollableFrame(self, label_text="")
        self._rows.grid(row=2, column=0, sticky="nsew", padx=14)
        self._rows.grid_columnconfigure(0, weight=1)

        if self._candidates:
            for candidate in self._candidates:
                self._add_row(candidate)
        else:
            ctk.CTkLabel(
                self._rows,
                text="No hosts found.\n\nVS Code Remote-SSH reads ~/.ssh/config; if "
                     "your hosts live elsewhere, point remote.SSH.configFile at it.",
                text_color=MUTED,
                justify="left",
            ).pack(pady=20)

        self._build_footer()

    def _add_row(self, candidate: ImportCandidate) -> None:
        host = candidate.host
        enabled = candidate.actionable
        selected = tk.BooleanVar(value=enabled)
        self._checks.append((candidate, selected))

        row = ctk.CTkFrame(self._rows, corner_radius=6)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(
            row,
            text="",
            variable=selected,
            width=24,
            state="normal" if enabled else "disabled",
        ).grid(row=0, column=0, padx=(10, 4), pady=8)

        details = ctk.CTkFrame(row, fg_color="transparent")
        details.grid(row=0, column=1, sticky="ew", padx=4)
        details.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            details,
            text=host.alias,
            font=ctk.CTkFont(weight="bold"),
            text_color=None if enabled else MUTED,
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            details,
            text=host.address,
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x")

        detail_bits = [candidate.reason]
        if host.identity_file:
            detail_bits.append(Path(host.identity_file).name)
        if host.proxy_jump:
            detail_bits.append(f"via {host.proxy_jump}")
        ctk.CTkLabel(
            details,
            text="   ·   ".join(detail_bits),
            font=ctk.CTkFont(size=11),
            text_color="gray50",
            anchor="w",
        ).pack(fill="x")

        label, color = _ACTION_LABELS.get(candidate.action, ("?", MUTED))
        ctk.CTkLabel(
            row,
            text=label,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=color,
            width=80,
        ).grid(row=0, column=2, padx=10)

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=14, pady=12)

        ctk.CTkButton(
            footer, text="Select all", width=90, height=28,
            command=lambda: self._select(True),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            footer, text="None", width=70, height=28,
            command=lambda: self._select(False),
        ).pack(side="left")

        ctk.CTkLabel(
            footer,
            text="Passwords are never read from the SSH config; key files are "
                 "referenced, not copied.",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
        ).pack(side="left", padx=14)

        self._btn_import = ctk.CTkButton(
            footer, text="Import", width=110, height=30,
            fg_color=SELECTED, command=self._confirm,
        )
        self._btn_import.pack(side="right")
        ctk.CTkButton(
            footer, text="Cancel", width=90, height=30,
            fg_color="gray40", command=self._cancel,
        ).pack(side="right", padx=6)

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


ApplyCallback = Callable[[ImportCandidate], None]
