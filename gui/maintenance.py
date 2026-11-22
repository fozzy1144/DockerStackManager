"""Docker housekeeping: what disk space is used, and reclaiming it safely.

Prune commands are the easiest way to lose data in Docker, so the destructive one
is separated visually, unchecked by default, and confirmed by name. Everything
here reports what it did into a log panel rather than a message box, because
``docker system prune`` output is worth reading.
"""

import re
import tkinter as tk
from dataclasses import dataclass

import customtkinter as ctk

from core.ssh_client import PRUNE_TARGETS, SSHClient
from gui import dialogs, theme
from gui.log_panel import LogPanel

#: Order shown in the dialog: safest first, data-destroying last.
_ORDER = (
    "dangling-images",
    "stopped-containers",
    "build-cache",
    "unused-images",
    "unused-volumes",
)


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One line of ``docker system df``."""

    kind: str
    total: str
    active: str
    size: str
    reclaimable: str


_DF_ROW = re.compile(
    r"^(Images|Containers|Local Volumes|Build Cache)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+?)\s*$"
)
_DF_LABELS = {"Local Volumes": "Volumes", "Build Cache": "Build cache"}


def parse_disk_usage(text: str) -> list[UsageRow]:
    """Rows of ``docker system df`` output, or ``[]`` if it is not that table.

    An empty result means "show the text as-is": it is usually an error message
    worth reading, such as Docker being unreachable.
    """
    rows = []
    for line in (text or "").splitlines():
        match = _DF_ROW.match(line.strip())
        if match:
            kind, total, active, size, reclaimable = match.groups()
            rows.append(UsageRow(_DF_LABELS.get(kind, kind), total, active, size, reclaimable))
    return rows


class MaintenanceWindow(ctk.CTkToplevel):
    """Disk usage and selective cleanup for one host."""

    def __init__(self, parent, ssh: SSHClient, host_name: str):
        super().__init__(parent)
        self._ssh = ssh
        self._busy = False
        self._choices: dict[str, tk.BooleanVar] = {}

        self.title(f"Cleanup — {host_name}")
        theme.present(self, parent, (980, 820), minsize=(800, 660))
        self._host_name = host_name

        self._build()
        self.after(120, self.refresh_usage)

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 12))
        header.grid_columnconfigure(0, weight=1)
        theme.overline(header, self._host_name).grid(row=0, column=0, sticky="w")
        theme.heading(header, "Docker disk usage", role="display").grid(row=1, column=0, sticky="w")
        self._btn_refresh = theme.button(header, "Refresh", self.refresh_usage, size="sm")
        self._btn_refresh.grid(row=1, column=1, sticky="e")

        self._usage = ctk.CTkFrame(self, fg_color="transparent", height=110)
        self._usage.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        self._usage.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="tiles")
        self._usage_placeholder("Reading disk usage…")

        self._build_choices()

        self._log = LogPanel(self, title="Output")
        self._log.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 20))

    def _build_choices(self) -> None:
        frame = theme.card(self)
        frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        frame.grid_columnconfigure(0, weight=1)

        theme.heading(frame, "Reclaim space", role="heading").grid(
            row=0, column=0, sticky="w", padx=16, pady=(14, 8)
        )

        safe = [key for key in _ORDER if not PRUNE_TARGETS[key][2]]
        risky = [key for key in _ORDER if PRUNE_TARGETS[key][2]]

        for index, key in enumerate(safe, start=1):
            self._choice_row(frame, key, index, destructive=False)

        if risky:
            # The data-destroying option lives in its own tinted box, so it can
            # never be mistaken for one more line in the safe list.
            zone_bg = theme.tint(theme.DANGER, 0.07, theme.SURFACE)
            zone = ctk.CTkFrame(
                frame, fg_color=zone_bg, corner_radius=10, border_width=1,
                border_color=theme.tint(theme.DANGER, 0.35, theme.SURFACE),
            )
            zone.grid(row=len(safe) + 1, column=0, sticky="ew", padx=12, pady=(10, 4))
            zone.grid_columnconfigure(0, weight=1)
            theme.overline(zone, "Danger zone", text_color=theme.DANGER).grid(
                row=0, column=0, sticky="w", padx=12, pady=(10, 0)
            )
            for index, key in enumerate(risky, start=1):
                self._choice_row(zone, key, index, destructive=True)

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=len(_ORDER) + 2, column=0, sticky="ew", padx=16, pady=(10, 14))
        theme.caption(
            actions,
            "Nothing here touches a running container or a volume still in use.",
            role="small",
        ).pack(side="left")
        self._btn_run = theme.button(actions, "Run selected cleanup", self._run, variant="danger")
        self._btn_run.pack(side="right")

    def _choice_row(self, parent, key: str, index: int, *, destructive: bool) -> None:
        command, description, _ = PRUNE_TARGETS[key]
        variable = tk.BooleanVar(value=False)
        self._choices[key] = variable

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=index, column=0, sticky="ew", padx=12, pady=(4, 8 if destructive else 4))
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(
            row, text=key.replace("-", " ").capitalize(), variable=variable, width=200,
            font=theme.font("body_strong"), checkbox_width=18, checkbox_height=18,
            text_color=theme.DANGER if destructive else theme.TEXT,
            fg_color=theme.DANGER_SOLID if destructive else theme.ACCENT,
            hover_color=theme.DANGER_SOLID_HOVER if destructive else theme.ACCENT_HOVER,
        ).grid(row=0, column=0, sticky="w")
        theme.caption(
            row, description, role="small",
            color=theme.WARNING if destructive else theme.TEXT_2,
        ).grid(row=0, column=1, sticky="w", padx=8)
        theme.text(row, command, "mono_small", theme.TEXT_3, anchor="e").grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )

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

        theme.run_in_thread(task)

    def _show_usage(self, usage: str) -> None:
        self._set_busy(False)
        rows = parse_disk_usage(usage)
        if not rows:
            self._usage_placeholder(usage.strip() or "No output.")
            return
        self._clear_usage()
        for column, row in enumerate(rows[:4]):
            self._usage_tile(column, row)

    def _clear_usage(self) -> None:
        for widget in self._usage.winfo_children():
            widget.destroy()

    def _usage_placeholder(self, message: str) -> None:
        self._clear_usage()
        box = theme.card(self._usage)
        box.grid(row=0, column=0, columnspan=4, sticky="ew")
        theme.caption(box, message, role="small", wraplength=880).pack(
            anchor="w", padx=16, pady=18
        )

    def _usage_tile(self, column: int, row: UsageRow) -> None:
        tile = theme.card(self._usage)
        tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
        theme.overline(tile, row.kind).pack(anchor="w", padx=16, pady=(14, 2))
        theme.text(tile, row.size, "display").pack(anchor="w", padx=16)
        theme.caption(tile, f"{row.total} total  ·  {row.active} active", role="small").pack(
            anchor="w", padx=16, pady=(2, 0)
        )
        reclaim = row.reclaimable.split(" ", 1)
        amount = reclaim[0]
        share = reclaim[1] if len(reclaim) > 1 else ""
        nothing = amount.rstrip("B").strip() in ("0", "0k", "0K", "0M", "0G")
        theme.caption(
            tile, f"{amount} reclaimable {share}".strip(), role="small_strong",
            color=theme.TEXT_3 if nothing else theme.ACCENT_TEXT,
        ).pack(anchor="w", padx=16, pady=(6, 14))

    def _run(self) -> None:
        if self._busy:
            return
        selected = [key for key in _ORDER if self._choices[key].get()]
        if not selected:
            dialogs.toast(self, "Nothing selected", "warning",
                          detail="Choose at least one thing to clean up.")
            return

        destructive = [key for key in selected if PRUNE_TARGETS[key][2]]
        prompt = "This will run:\n\n" + "\n".join(
            f"  •  {PRUNE_TARGETS[k][0]}" for k in selected
        )
        if destructive:
            prompt += (
                "\n\nWARNING: 'unused-volumes' permanently deletes any volume no "
                "container currently references. A stack that is merely stopped "
                "counts as not referencing its volumes.\n\nProceed?"
            )
        if not dialogs.askyesno(
            "Delete volumes too?" if destructive else "Run cleanup?",
            prompt, parent=self,
            confirm="Delete and clean up" if destructive else "Clean up",
            tone="danger" if destructive else "question",
        ):
            return

        self._set_busy(True)
        self._log.log(f"Cleaning up: {', '.join(selected)}")

        def task() -> None:
            ok = self._ssh.prune(selected, self._log.log)
            self.after(0, lambda: self._after_run(ok))

        theme.run_in_thread(task)

    def _after_run(self, ok: bool) -> None:
        self._set_busy(False)
        self._log.log(
            "Cleanup finished." if ok else "Cleanup finished with errors.",
            "success" if ok else "warn",
        )
        dialogs.toast(self, "Cleanup finished" if ok else "Cleanup finished with errors",
                      "success" if ok else "warning")
        self.refresh_usage()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._btn_run.configure(state=state)
        self._btn_refresh.configure(state=state)
        self.configure(cursor="watch" if busy else "")
