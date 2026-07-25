"""Per-stack control window: containers, lifecycle actions, and live logs.

One window per stack rather than one per feature, because the three things you
want while fixing a stack — what is running, what the logs say, and a button to
restart it — are the same three things every time.

Log following runs as a real remote process held in :attr:`_process`, so Stop
actually terminates ``docker compose logs -f`` instead of leaving it attached to
an abandoned channel.
"""

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk

from core.ssh_client import RemoteProcess, SSHClient
from gui.log_panel import LogPanel
from gui.theme import (
    ACCENT_PURPLE,
    ACCENT_PURPLE_HOVER,
    DANGER,
    DANGER_HOVER,
    MUTED,
    SELECTED,
    state_color,
)
from models.host import Container, DockerStack

TAIL_CHOICES = ("100", "500", "2000", "all")

#: Lifecycle buttons: label, action name, whether it needs confirming, colours.
_ACTIONS: tuple[tuple[str, str, bool, str, str], ...] = (
    ("Up", "up", False, "", ""),
    ("Restart", "restart", False, "", ""),
    ("Stop", "stop", True, "", ""),
    ("Pull", "pull", False, "", ""),
    ("Recreate", "recreate", True, ACCENT_PURPLE, ACCENT_PURPLE_HOVER),
    ("Down", "down", True, DANGER, DANGER_HOVER),
)

_CONFIRMATIONS = {
    "stop": "Stop every container in '{name}'?\n\nThe stack stays defined and can "
            "be started again.",
    "down": "Take '{name}' down?\n\nContainers and the stack's network are removed. "
            "Named volumes are kept.",
    "recreate": "Recreate every container in '{name}'?\n\nContainers are destroyed "
                "and rebuilt from the compose file, even if nothing changed.",
}


class StackWindow(ctk.CTkToplevel):
    """Containers, actions, and logs for a single stack."""

    def __init__(
        self,
        parent,
        ssh: SSHClient,
        stack: DockerStack,
        on_changed: Optional[Callable[[], None]] = None,
        on_edit: Optional[Callable[[DockerStack], None]] = None,
    ):
        super().__init__(parent)
        self._ssh = ssh
        self._stack = stack
        self._on_changed = on_changed
        self._on_edit = on_edit

        self._process: Optional[RemoteProcess] = None
        self._containers: list[Container] = []
        self._busy = False
        self._service_filter = tk.StringVar(value="(all services)")
        self._tail = tk.StringVar(value="500")
        self._follow = tk.BooleanVar(value=True)

        self.title(f"Stack — {stack.name}")
        self.geometry("1120x780")
        self.minsize(880, 600)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(80, self._focus_window)
        self.after(120, self.refresh)

    def _focus_window(self) -> None:
        self.lift()
        self.focus_force()

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(4, weight=2)

        self._build_header()
        self._build_actions()

        self._container_list = ctk.CTkScrollableFrame(self, label_text="")
        self._container_list.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 4))
        self._container_list.grid_columnconfigure(0, weight=1)

        self._build_log_controls()

        self._log = LogPanel(self)
        self._log.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 2))

        ctk.CTkLabel(
            header, text=self._stack.name, font=ctk.CTkFont(size=17, weight="bold")
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=self._stack.path, font=ctk.CTkFont(size=11), text_color=MUTED
        ).pack(side="left", padx=10)

        self._summary = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=12))
        self._summary.pack(side="right")

    def _build_actions(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))

        self._buttons: list[ctk.CTkButton] = []
        for label, action, confirm, color, hover in _ACTIONS:
            extra = {}
            if color:
                extra = {"fg_color": color, "hover_color": hover}
            button = ctk.CTkButton(
                row,
                text=label,
                width=88,
                height=28,
                command=lambda a=action, c=confirm: self._run_action(a, c),
                **extra,
            )
            button.pack(side="left", padx=3)
            self._buttons.append(button)

        self._btn_edit = ctk.CTkButton(
            row, text="Edit compose", width=112, height=28, command=self._edit
        )
        self._btn_edit.pack(side="left", padx=(14, 3))
        self._buttons.append(self._btn_edit)

        self._btn_refresh = ctk.CTkButton(
            row, text="Refresh", width=82, height=28, command=self.refresh
        )
        self._btn_refresh.pack(side="right", padx=3)
        self._buttons.append(self._btn_refresh)

    def _build_log_controls(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 2))

        ctk.CTkLabel(row, text="Logs", font=ctk.CTkFont(weight="bold")).pack(side="left")

        self._service_menu = ctk.CTkOptionMenu(
            row, values=["(all services)"], variable=self._service_filter, width=180
        )
        self._service_menu.pack(side="left", padx=(12, 4))

        ctk.CTkLabel(row, text="tail", font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkOptionMenu(
            row, values=list(TAIL_CHOICES), variable=self._tail, width=90
        ).pack(side="left", padx=4)

        ctk.CTkCheckBox(row, text="follow", variable=self._follow, width=80).pack(
            side="left", padx=6
        )

        self._btn_stop = ctk.CTkButton(
            row, text="Stop", width=74, height=26, command=self._stop_logs,
            state="disabled", fg_color=DANGER, hover_color=DANGER_HOVER,
        )
        self._btn_stop.pack(side="right", padx=3)

        self._btn_show = ctk.CTkButton(
            row, text="Show logs", width=98, height=26, command=self._start_logs
        )
        self._btn_show.pack(side="right", padx=3)

    # ──────────────────────────────────────────────────────────────────────────
    # Containers
    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-read the stack's container list from the host."""
        if self._busy:
            return
        self._set_busy(True)
        self._summary.configure(text="reading…", text_color=MUTED)

        def task() -> None:
            containers = self._ssh.compose_ps(self._stack)
            self.after(0, lambda: self._show_containers(containers))

        _in_thread(task)

    def _show_containers(self, containers: list[Container]) -> None:
        self._set_busy(False)
        self._containers = containers

        for widget in self._container_list.winfo_children():
            widget.destroy()

        if not containers:
            ctk.CTkLabel(
                self._container_list,
                text="No containers. The stack is defined but has never been "
                     "started, or Docker is unreachable.",
                text_color=MUTED,
                anchor="w",
                justify="left",
            ).pack(fill="x", padx=6, pady=10)
            self._summary.configure(text="0 containers", text_color=MUTED)
        else:
            for container in containers:
                self._add_container_row(container)
            running = sum(1 for c in containers if c.is_running)
            total = len(containers)
            self._summary.configure(
                text=f"{running}/{total} running",
                text_color=state_color("running" if running == total else "partial"),
            )

        services = ["(all services)"] + sorted(
            {c.service for c in containers if c.service}
        )
        self._service_menu.configure(values=services)
        if self._service_filter.get() not in services:
            self._service_filter.set("(all services)")

    def _add_container_row(self, container: Container) -> None:
        row = ctk.CTkFrame(self._container_list, corner_radius=6)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            row,
            text=container.status_label,
            text_color=state_color(container.health or container.state),
            font=ctk.CTkFont(size=11, weight="bold"),
            width=130,
            anchor="w",
        ).grid(row=0, column=0, padx=(10, 4), pady=6, sticky="w")

        details = ctk.CTkFrame(row, fg_color="transparent")
        details.grid(row=0, column=1, sticky="ew", padx=4)
        details.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            details,
            text=container.service or container.name,
            font=ctk.CTkFont(weight="bold"),
            anchor="w",
        ).pack(fill="x")
        subtitle = container.name
        if container.image:
            subtitle += f"   ·   {container.image}"
        ctk.CTkLabel(
            details, text=subtitle, font=ctk.CTkFont(size=11),
            text_color=MUTED, anchor="w",
        ).pack(fill="x")

        ctk.CTkLabel(
            row,
            text=container.ports or "no published ports",
            font=ctk.CTkFont(size=11),
            text_color=MUTED if container.ports else "gray45",
            anchor="e",
        ).grid(row=0, column=2, padx=8)

        if container.service:
            ctk.CTkButton(
                row,
                text="logs",
                width=54,
                height=24,
                fg_color=SELECTED,
                command=lambda s=container.service: self._logs_for(s),
            ).grid(row=0, column=3, padx=(4, 10))

    def _logs_for(self, service: str) -> None:
        """Filter to one service and start streaming immediately."""
        self._service_filter.set(service)
        self._start_logs()

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle actions
    # ──────────────────────────────────────────────────────────────────────────

    def _run_action(self, action: str, confirm: bool) -> None:
        if self._busy:
            return
        prompt = _CONFIRMATIONS.get(action)
        if confirm and prompt and not messagebox.askyesno(
            f"Confirm {action}",
            prompt.format(name=self._stack.name),
            parent=self,
        ):
            return

        self._set_busy(True)
        self._log.log(f"Running '{action}' on {self._stack.name}…")

        def task() -> None:
            ok = self._ssh.compose_action(self._stack, action, self._log.log)
            self.after(0, lambda: self._after_action(action, ok))

        _in_thread(task)

    def _after_action(self, action: str, ok: bool) -> None:
        self._set_busy(False)
        self._log.log(
            f"'{action}' {'finished' if ok else 'failed'}.",
            "success" if ok else "error",
        )
        if self._on_changed is not None:
            self._on_changed()
        self.refresh()

    def _edit(self) -> None:
        if self._on_edit is not None:
            self._on_edit(self._stack)

    # ──────────────────────────────────────────────────────────────────────────
    # Log streaming
    # ──────────────────────────────────────────────────────────────────────────

    def _start_logs(self) -> None:
        self._stop_logs()
        service = self._service_filter.get()
        if service == "(all services)":
            service = ""

        follow = self._follow.get()
        tail = self._tail.get()
        self._log.log(
            f"── logs: {service or 'all services'}, tail={tail}"
            f"{', following' if follow else ''} ──"
        )
        self._btn_show.configure(state="disabled")
        self._btn_stop.configure(state="normal" if follow else "disabled")

        def task() -> None:
            process, error = self._ssh.start_logs(
                self._stack, service=service, tail=tail, follow=follow
            )
            if process is None:
                self.after(0, lambda: self._logs_finished(error))
                return
            self._process = process
            process.pump(self._log.log, timeout=_LOG_TIMEOUT)
            self.after(0, lambda: self._logs_finished(""))

        _in_thread(task)

    def _stop_logs(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.stop()
        self._btn_stop.configure(state="disabled")
        self._btn_show.configure(state="normal")

    def _logs_finished(self, error: str) -> None:
        self._process = None
        self._btn_stop.configure(state="disabled")
        self._btn_show.configure(state="normal")
        if error:
            self._log.log(error, "error")

    # ──────────────────────────────────────────────────────────────────────────
    # Housekeeping
    # ──────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._buttons:
            button.configure(state=state)
        self.configure(cursor="watch" if busy else "")

    def _close(self) -> None:
        self._stop_logs()
        self.destroy()


_LOG_TIMEOUT = 86400


def _in_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()
