"""Docker housekeeping: what disk space is used, and reclaiming it safely.

Prune commands are the easiest way to lose data in Docker, so the destructive one
is separated visually, unchecked by default, and confirmed by name. Everything
here reports what it did into a log panel rather than a message box, because
``docker system prune`` output is worth reading.
"""

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from core.ssh_client import PRUNE_TARGETS, SSHClient
from gui.log_panel import LogPanel
from gui.theme import DANGER, DANGER_HOVER, LEVEL_ACCENTS, MUTED

#: Order shown in the dialog: safest first, data-destroying last.
_ORDER = (
    "dangling-images",
    "stopped-containers",
    "build-cache",
    "unused-images",
    "unused-volumes",
)


class MaintenanceWindow(ctk.CTkToplevel):
    """Disk usage and selective cleanup for one host."""

    def __init__(self, parent, ssh: SSHClient, host_name: str):
        super().__init__(parent)
        self._ssh = ssh
        self._busy = False
        self._choices: dict[str, tk.BooleanVar] = {}

        self.title(f"Docker Maintenance — {host_name}")
        self.geometry("940x760")
        self.minsize(760, 620)

        self._build()
        self.after(80, self._focus_window)
        self.after(120, self.refresh_usage)

    def _focus_window(self) -> None:
        self.lift()
        self.focus_force()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 2))
        ctk.CTkLabel(
            header, text="Disk usage", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(side="left")
        self._btn_refresh = ctk.CTkButton(
            header, text="Refresh", width=88, height=26, command=self.refresh_usage
        )
        self._btn_refresh.pack(side="right")

        self._usage = ctk.CTkTextbox(
            self, font=("Consolas", 12), wrap="none", height=150, state="disabled"
        )
        self._usage.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        self._build_choices()

        self._log = LogPanel(self)
        self._log.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 10))

    def _build_choices(self) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame, text="Reclaim space", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        for index, key in enumerate(_ORDER, start=1):
            command, description, destructive = PRUNE_TARGETS[key]
            variable = tk.BooleanVar(value=False)
            self._choices[key] = variable

            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", padx=12, pady=1)
            row.grid_columnconfigure(1, weight=1)

            ctk.CTkCheckBox(
                row, text=key.replace("-", " "), variable=variable, width=190,
                text_color=LEVEL_ACCENTS["error"] if destructive else None,
            ).grid(row=0, column=0, sticky="w")

            ctk.CTkLabel(
                row,
                text=description,
                font=ctk.CTkFont(size=11),
                text_color=LEVEL_ACCENTS["warning"] if destructive else MUTED,
                anchor="w",
            ).grid(row=1 if destructive else 0, column=1, sticky="w", padx=8)

            ctk.CTkLabel(
                row, text=command, font=ctk.CTkFont(size=10), text_color="gray45",
                anchor="e",
            ).grid(row=0, column=2, sticky="e", padx=6)

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=len(_ORDER) + 1, column=0, sticky="ew", padx=12, pady=(6, 10))

        self._btn_run = ctk.CTkButton(
            actions,
            text="Run selected cleanup",
            command=self._run,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
        )
        self._btn_run.pack(side="right")

        ctk.CTkLabel(
            actions,
            text="Nothing here touches a running container or a volume still in use.",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
        ).pack(side="left")

    # ──────────────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────────────

    def refresh_usage(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def task() -> None:
            usage = self._ssh.disk_usage()
            self.after(0, lambda: self._show_usage(usage))

        _in_thread(task)

    def _show_usage(self, usage: str) -> None:
        self._set_busy(False)
        self._usage.configure(state="normal")
        self._usage.delete("1.0", "end")
        self._usage.insert("1.0", usage or "No output.")
        self._usage.configure(state="disabled")

    def _run(self) -> None:
        if self._busy:
            return
        selected = [key for key in _ORDER if self._choices[key].get()]
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Choose at least one thing to clean up.", parent=self
            )
            return

        destructive = [key for key in selected if PRUNE_TARGETS[key][2]]
        prompt = "Run cleanup for:\n\n" + "\n".join(f"  • {k}" for k in selected)
        if destructive:
            prompt += (
                "\n\nWARNING: 'unused-volumes' permanently deletes any volume no "
                "container currently references. A stack that is merely stopped "
                "counts as not referencing its volumes.\n\nProceed?"
            )
        if not messagebox.askyesno("Confirm cleanup", prompt, parent=self):
            return

        self._set_busy(True)
        self._log.log(f"Cleaning up: {', '.join(selected)}")

        def task() -> None:
            ok = self._ssh.prune(selected, self._log.log)
            self.after(0, lambda: self._after_run(ok))

        _in_thread(task)

    def _after_run(self, ok: bool) -> None:
        self._set_busy(False)
        self._log.log(
            "Cleanup finished." if ok else "Cleanup finished with errors.",
            "success" if ok else "warn",
        )
        self.refresh_usage()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._btn_run.configure(state=state)
        self._btn_refresh.configure(state=state)
        self.configure(cursor="watch" if busy else "")


def _in_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()
