"""Per-stack control window: containers, lifecycle actions, and live logs.

One window per stack rather than one per feature, because the three things you
want while fixing a stack — what is running, what the logs say, and a button to
restart it — are the same three things every time.

Log following runs as a real remote process held in :attr:`_process`, so Stop
actually terminates ``docker compose logs -f`` instead of leaving it attached to
an abandoned channel.
"""

import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from core.ssh_client import RemoteProcess, SSHClient
from gui import dialogs, theme
from gui.log_panel import LogPanel
from models.host import Container, DockerStack, ImageStatus

TAIL_CHOICES = ("100", "500", "2000", "all")

#: Lifecycle buttons: label, action name, whether it needs confirming, and the
#: button variant — the more disruptive the action, the louder the button.
_ACTIONS: tuple[tuple[str, str, bool, str], ...] = (
    ("Up", "up", False, "secondary"),
    ("Restart", "restart", False, "secondary"),
    ("Stop", "stop", True, "secondary"),
    ("Pull", "pull", False, "secondary"),
    ("Recreate", "recreate", True, "warning_soft"),
    ("Down", "down", True, "danger_soft"),
)

#: Confirm-button verb and dialog tone per confirmed action.
_CONFIRM_STYLE = {
    "stop": ("Stop", "warning"),
    "recreate": ("Recreate", "warning"),
    "down": ("Take down", "danger"),
}

_CONFIRMATIONS = {
    "stop": "Every container in {name} stops. The stack stays defined and can be "
            "started again with Up.",
    "down": "The containers and network of {name} are removed. Named volumes are kept, "
            "so Up brings it back with its data.",
    "recreate": "Every container in {name} is destroyed and rebuilt from the compose "
                "file, even if nothing changed.",
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
        self._busy = False
        self._service_filter = tk.StringVar(value="(all services)")
        self._tail = tk.StringVar(value="500")
        self._follow = tk.BooleanVar(value=True)

        self.title(f"{stack.name} — Stack")
        theme.present(self, parent, (1180, 820), minsize=(920, 640))

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<F5>", lambda _e: self.refresh())
        self.after(120, self.refresh)

    # ──────────────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=2)
        self.grid_rowconfigure(3, weight=3)

        self._build_header()
        self._build_actions()

        containers = theme.card(self)
        containers.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 10))
        containers.grid_columnconfigure(0, weight=1)
        containers.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(containers, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        theme.heading(head, "Containers", role="heading").pack(side="left")
        self._count = theme.caption(head, "", role="small")
        self._count.pack(side="left", padx=(8, 0))

        self._container_list = theme.scrollable(containers)
        self._container_list.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        self._container_list.grid_columnconfigure(0, weight=1)

        self._log = LogPanel(self, title="Logs", filters=False)
        self._log.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 20))
        self._build_log_controls(self._log.toolbar)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        header.grid_columnconfigure(0, weight=1)

        theme.overline(header, "Stack").grid(row=0, column=0, sticky="w")
        theme.heading(header, self._stack.name, role="display").grid(row=1, column=0, sticky="w")
        theme.text(header, self._stack.path, "mono_small", theme.TEXT_3).grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )

        self._summary = theme.pill(header, "", theme.TEXT_3, base=theme.BG)
        self._summary.grid(row=1, column=1, sticky="e")

    def _build_actions(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 14))

        self._buttons: list[ctk.CTkButton] = []

        def add(widget: ctk.CTkButton, gap: int = 6, tip: str = "") -> ctk.CTkButton:
            widget.pack(side="left", padx=(0, gap))
            self._buttons.append(widget)
            if tip:
                theme.Tooltip(widget, tip)
            return widget

        for label, action, confirm, variant in _ACTIONS:
            add(theme.button(
                row, label, lambda a=action, c=confirm: self._run_action(a, c),
                variant=variant, size="sm",
            ))

        theme.divider(row, vertical=True, color=theme.BORDER_STRONG).pack(
            side="left", padx=(6, 12), fill="y", pady=4
        )

        add(theme.button(row, "Check images", self._check_images, size="sm"),
            tip="Ask each registry whether a newer image exists")
        add(theme.button(row, "Roll back", self._roll_back, variant="danger_soft", size="sm"),
            tip="Restore the image versions from before the last update")
        add(theme.button(row, "Edit compose", self._edit, size="sm"))

        self._btn_refresh = theme.button(row, "Refresh", self.refresh, variant="ghost", size="sm")
        self._btn_refresh.pack(side="right")
        self._buttons.append(self._btn_refresh)
        theme.Tooltip(self._btn_refresh, "Re-read containers  (F5)")

    def _build_log_controls(self, row) -> None:
        self._service_menu = ctk.CTkOptionMenu(
            row, values=["(all services)"], variable=self._service_filter, width=170,
            height=28, font=theme.font("small"), dropdown_font=theme.font("small"),
        )
        self._service_menu.pack(side="left")

        theme.caption(row, "tail", role="small").pack(side="left", padx=(12, 6))
        ctk.CTkOptionMenu(
            row, values=list(TAIL_CHOICES), variable=self._tail, width=78, height=28,
            font=theme.font("small"), dropdown_font=theme.font("small"),
        ).pack(side="left")

        ctk.CTkSwitch(
            row, text="Follow", variable=self._follow, font=theme.font("small"),
            switch_width=32, switch_height=16, width=80,
        ).pack(side="left", padx=(14, 0))

        self._btn_stop = theme.button(
            row, "Stop", self._stop_logs, variant="danger_soft", size="sm", state="disabled",
        )
        self._btn_stop.pack(side="right", padx=(4, 4))

        self._btn_show = theme.button(
            row, "Show logs", self._start_logs, variant="primary", size="sm",
        )
        self._btn_show.pack(side="right")

    # ──────────────────────────────────────────────────────────────────────────
    # Containers
    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-read the stack's container list from the host."""
        if self._busy:
            return
        self._set_busy(True)
        theme.set_pill(self._summary, "Reading…", theme.TEXT_3, base=theme.BG)

        def task() -> None:
            containers = self._ssh.compose_ps(self._stack)
            self.after(0, lambda: self._show_containers(containers))

        theme.run_in_thread(task)

    def _show_containers(self, containers: list[Container]) -> None:
        self._set_busy(False)

        for widget in self._container_list.winfo_children():
            widget.destroy()

        if not containers:
            theme.caption(
                self._container_list,
                "No containers. The stack is defined but has never been started, "
                "or Docker is unreachable.",
                role="small",
            ).pack(fill="x", padx=8, pady=14)
            theme.set_pill(self._summary, "No containers", theme.TEXT_3, base=theme.BG)
            self._count.configure(text="")
        else:
            for container in containers:
                self._add_container_row(container)
            running = sum(1 for c in containers if c.is_running)
            total = len(containers)
            state = "running" if running == total else "stopped" if not running else "partial"
            theme.set_pill(
                self._summary, f"{running} of {total} running", theme.state_color(state),
                base=theme.BG,
            )
            self._count.configure(text=str(total))

        services = ["(all services)"] + sorted(
            {c.service for c in containers if c.service}
        )
        self._service_menu.configure(values=services)
        if self._service_filter.get() not in services:
            self._service_filter.set("(all services)")

    def _add_container_row(self, container: Container) -> None:
        row = ctk.CTkFrame(
            self._container_list, corner_radius=10, fg_color=theme.SURFACE_2,
            border_width=1, border_color=theme.BORDER,
        )
        row.pack(fill="x", pady=3, padx=(0, 4))
        row.grid_columnconfigure(1, weight=1)
        self.after_idle(lambda: theme.hover_row(row))

        theme.pill(
            row, container.status_label,
            theme.state_color(container.health or container.state),
            width=150, anchor="w",
        ).grid(row=0, column=0, padx=(12, 6), pady=12, sticky="w")

        details = ctk.CTkFrame(row, fg_color="transparent")
        details.grid(row=0, column=1, sticky="ew", padx=6, pady=8)
        theme.text(details, container.service or container.name, "body_strong").pack(fill="x")
        subtitle = container.name
        if container.image:
            subtitle += f"  ·  {container.image}"
        theme.text(details, subtitle, "mono_small", theme.TEXT_3).pack(fill="x", pady=(1, 0))

        theme.text(
            row,
            container.ports or "no published ports",
            "mono_small",
            theme.TEXT_2 if container.ports else theme.TEXT_DISABLED,
            anchor="e",
        ).grid(row=0, column=2, padx=10)

        if container.service:
            theme.button(
                row, "Logs", lambda s=container.service: self._logs_for(s),
                variant="ghost", size="sm",
            ).grid(row=0, column=3, padx=(0, 10))

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
        verb, tone = _CONFIRM_STYLE.get(action, (action.capitalize(), "warning"))
        if confirm and prompt and not dialogs.askyesno(
            f"{verb} {self._stack.name}?",
            prompt.format(name=self._stack.name),
            parent=self, confirm=verb, tone=tone,
        ):
            return

        self._set_busy(True)
        self._log.log(f"Running '{action}' on {self._stack.name}…")

        def task() -> None:
            ok = self._ssh.compose_action(self._stack, action, self._log.log)
            self.after(0, lambda: self._after_action(action, ok))

        theme.run_in_thread(task)

    def _after_action(self, action: str, ok: bool) -> None:
        self._set_busy(False)
        self._log.log(
            f"'{action}' {'finished' if ok else 'failed'}.",
            "success" if ok else "error",
        )
        dialogs.toast(
            self, f"{action.capitalize()} {'finished' if ok else 'failed'}",
            "success" if ok else "error",
            detail="" if ok else "See the log below for the output.",
        )
        if self._on_changed is not None:
            self._on_changed()
        self.refresh()

    def _edit(self) -> None:
        if self._on_edit is not None:
            self._on_edit(self._stack)

    # ──────────────────────────────────────────────────────────────────────────
    # Image freshness and rollback
    # ──────────────────────────────────────────────────────────────────────────

    def _check_images(self) -> None:
        """Ask each image's registry whether a newer version exists."""
        if self._busy:
            return
        self._set_busy(True)
        self._log.log(f"Checking image versions for {self._stack.name}…")

        def task() -> None:
            statuses = self._ssh.check_stack_images(self._stack)
            self.after(0, lambda: self._show_image_statuses(statuses))

        theme.run_in_thread(task)

    def _show_image_statuses(self, statuses: list[ImageStatus]) -> None:
        self._set_busy(False)
        if not statuses:
            self._log.log(
                "No images found — is this a valid compose file, and is Docker "
                "reachable?",
                "warn",
            )
            return

        outdated = 0
        for status in statuses:
            if status.update_available:
                outdated += 1
                level = "warn"
            elif status.update_available is None:
                level = "info"
            else:
                level = "success"
            suffix = f" ({status.detail})" if status.detail else ""
            self._log.log(f"{status.image}: {status.label}{suffix}", level)

        unknown = sum(1 for s in statuses if s.update_available is None)
        summary = f"{outdated} of {len(statuses)} image(s) have updates"
        if unknown:
            summary += f", {unknown} could not be determined"
        self._log.log(summary, "warn" if outdated else "success")
        dialogs.toast(
            self,
            f"{outdated} image update{'' if outdated == 1 else 's'} available" if outdated
            else "Images are up to date",
            "warning" if outdated else "success",
            detail=f"{unknown} could not be determined" if unknown else "",
        )

    def _roll_back(self) -> None:
        """Restore the image versions recorded before the last pull."""
        if self._busy:
            return
        snapshot = dict(self._stack.image_snapshot)
        if not snapshot:
            dialogs.showinfo(
                "Nothing to roll back to",
                "No previous image versions have been recorded for this stack.\n\n"
                "A rollback point is created automatically the next time the stack "
                "is updated from this application.",
                parent=self,
            )
            return

        taken = self._stack.snapshot_taken or "an earlier update"
        listing = "\n".join(f"  • {image}" for image in sorted(snapshot))
        if not dialogs.askyesno(
            "Roll back images?",
            f"The image versions recorded at {taken} will be restored and the "
            f"containers recreated from them.\n\n{listing}",
            parent=self, confirm="Roll back", tone="warning",
        ):
            return

        self._set_busy(True)

        def task() -> None:
            ok = self._ssh.rollback_stack(self._stack, snapshot, self._log.log)
            self.after(0, lambda: self._after_rollback(ok))

        theme.run_in_thread(task)

    def _after_rollback(self, ok: bool) -> None:
        self._set_busy(False)
        self._log.log(
            "Rollback complete." if ok else "Rollback did not complete.",
            "success" if ok else "error",
        )
        dialogs.toast(self, "Rolled back" if ok else "Rollback failed",
                      "success" if ok else "error")
        if ok and self._on_changed is not None:
            self._on_changed()
        self.refresh()

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

        theme.run_in_thread(task)

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
