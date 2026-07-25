"""Main application window.

The window owns the host list, one optional SSH session to the *selected* host,
and the output log. Anything that touches the network runs on a worker thread
and comes back through :meth:`tkinter.Misc.after`; log lines are the exception,
since :class:`~gui.log_panel.LogPanel` is itself thread-safe.

Bulk actions ("all hosts") each open their own short-lived connection, so they
are independent of whatever the selected host is doing.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from core import distro, ssh_config
from core.credentials import (
    delete_password,
    flush_pending_saves,
    get_password,
    load_hosts,
    save_hosts,
    save_hosts_async,
    save_password,
)
from core.ssh_client import SSHClient
from gui.compose_editor import ComposeEditor
from gui.host_dialog import HostDialog
from gui.host_list import HostList, os_badge_color
from gui.import_dialog import ImportHostsDialog
from gui.import_dialog import summarize as import_summary
from gui.log_panel import LogPanel
from gui.maintenance import MaintenanceWindow
from gui.stack_window import StackWindow
from gui.theme import (
    ACCENT_BLUE,
    ACCENT_BLUE_HOVER,
    ACCENT_PURPLE,
    ACCENT_PURPLE_HOVER,
    ACCENT_TEAL,
    ACCENT_TEAL_HOVER,
    MUTED,
    state_color,
)
from models.host import UPDATES_FAILED, UPDATES_UNKNOWN, DockerStack, Host

MAX_PARALLEL_HOSTS = 8
"""Cap on concurrent SSH sessions during a bulk action.

One thread per host stops being an optimisation somewhere around a dozen hosts;
past that it is just contention and a fan of simultaneous auth attempts.
"""

#: Icon per stack status; the colour comes from :func:`gui.theme.state_color`.
STACK_ICONS = {"running": "●", "partial": "◑", "stopped": "○", "unknown": "?"}

Logger = Callable[..., None]
HostWorker = Callable[[Host, SSHClient, Logger], None]


class App(ctk.CTk):
    """The application window."""

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Docker & System Update Manager")
        self.geometry("1200x760")
        self.minsize(900, 600)

        self._hosts, skipped = _read_hosts()
        self._active: Optional[Host] = None
        self._ssh: Optional[SSHClient] = None
        self._stack_vars: dict[str, tk.BooleanVar] = {}
        self._windows: dict[str, ctk.CTkToplevel] = {}
        self._busy = False
        self._connecting = False
        self._connect_token = 0
        """Incremented to abandon an in-flight connection attempt."""

        self._build_ui()
        self._host_list.set_hosts(self._hosts)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log.log(f"Loaded {len(self._hosts)} host(s).")
        if skipped:
            self._log.log(
                f"Ignored {skipped} unreadable host entr"
                f"{'y' if skipped == 1 else 'ies'} in the config file.",
                "warn",
            )

    # ──────────────────────────────────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self._build_header(content)

        split = ctk.CTkFrame(content, fg_color="transparent")
        split.grid(row=1, column=0, sticky="nsew")
        split.grid_rowconfigure(0, weight=1)
        split.grid_columnconfigure(0, weight=2)
        split.grid_columnconfigure(1, weight=3)

        self._build_stack_panel(split)

        self._log = LogPanel(split)
        self._log.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=240, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            sidebar, text="Hosts", font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, padx=12, pady=(14, 4), sticky="w")

        self._host_list = HostList(
            sidebar,
            on_select=self._select_host,
            on_edit=self._edit_host,
            on_remove=self._remove_host,
        )
        self._host_list.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        add_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        add_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 0))
        add_row.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(add_row, text="+ Add Host", command=self._add_host).grid(
            row=0, column=0, sticky="ew"
        )
        ctk.CTkButton(
            add_row,
            text="Import…",
            width=76,
            fg_color=ACCENT_TEAL,
            hover_color=ACCENT_TEAL_HOVER,
            command=self._import_from_ssh_config,
        ).grid(row=0, column=1, padx=(4, 0))

        bulk = ctk.CTkFrame(sidebar, fg_color="transparent")
        bulk.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        self._btn_check_all = ctk.CTkButton(
            bulk,
            text="Check All Updates",
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_HOVER,
            command=self._check_all_updates,
        )
        self._btn_check_all.pack(fill="x", pady=(0, 4))
        self._btn_update_all = ctk.CTkButton(
            bulk,
            text="Update All Hosts",
            fg_color=ACCENT_PURPLE,
            hover_color=ACCENT_PURPLE_HOVER,
            command=self._update_all_hosts,
        )
        self._btn_update_all.pack(fill="x")

    def _build_header(self, parent) -> None:
        bar = ctk.CTkFrame(parent, height=60)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        self._host_title = ctk.CTkLabel(
            bar, text="Select a host", font=ctk.CTkFont(size=18, weight="bold")
        )
        self._host_title.grid(row=0, column=0, padx=14, pady=4, sticky="w")

        self._os_badge = ctk.CTkLabel(
            bar,
            text="",
            fg_color=distro.DEFAULT_BRAND_COLOR,
            corner_radius=8,
            padx=8,
        )
        self._os_badge.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=2, padx=8, pady=4, sticky="e")

        self._btn_connect = ctk.CTkButton(
            actions, text="Connect", width=100, command=self._toggle_connect
        )
        self._btn_connect.pack(side="left", padx=4)

        self._btn_scan = ctk.CTkButton(
            actions,
            text="Scan Stacks",
            width=110,
            command=self._scan_stacks,
            state="disabled",
        )
        self._btn_scan.pack(side="left", padx=4)

        self._btn_sys_update = ctk.CTkButton(
            actions,
            text="System Update",
            width=120,
            fg_color=ACCENT_PURPLE,
            hover_color=ACCENT_PURPLE_HOVER,
            command=self._run_system_update,
            state="disabled",
        )
        self._btn_sys_update.pack(side="left", padx=4)

        self._btn_maintenance = ctk.CTkButton(
            actions,
            text="Cleanup",
            width=94,
            fg_color=ACCENT_TEAL,
            hover_color=ACCENT_TEAL_HOVER,
            command=self._open_maintenance,
            state="disabled",
        )
        self._btn_maintenance.pack(side="left", padx=4)

    def _build_stack_panel(self, parent) -> None:
        panel = ctk.CTkFrame(parent)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(
            header, text="Docker Stacks", font=ctk.CTkFont(weight="bold")
        ).pack(side="left")

        toggles = ctk.CTkFrame(header, fg_color="transparent")
        toggles.pack(side="right")
        ctk.CTkButton(
            toggles, text="All", width=44, height=24,
            command=lambda: self._select_all_stacks(True),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            toggles, text="None", width=44, height=24,
            command=lambda: self._select_all_stacks(False),
        ).pack(side="left", padx=2)

        self._stack_scroll = ctk.CTkScrollableFrame(panel, label_text="")
        self._stack_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._stack_scroll.grid_columnconfigure(0, weight=1)

        self._btn_update_stacks = ctk.CTkButton(
            panel,
            text="Update Selected Stacks",
            state="disabled",
            command=self._update_selected_stacks,
        )
        self._btn_update_stacks.grid(row=2, column=0, padx=8, pady=8, sticky="ew")

    # ──────────────────────────────────────────────────────────────────────────
    # Host management
    # ──────────────────────────────────────────────────────────────────────────

    def _add_host(self) -> None:
        result = self._ask_host()
        if not result:
            return
        host = Host(
            hostname=result["hostname"],
            username=result["username"],
            port=result["port"],
            label=result["label"],
            key_path=result["key_path"],
        )
        if result["password"]:
            save_password(host.hostname, host.username, result["password"])
        self._hosts.append(host)
        self._save_hosts_soon()
        self._host_list.set_hosts(self._hosts)
        self._select_host(host)

    def _edit_host(self, host: Host) -> None:
        result = self._ask_host(host)
        if not result:
            return

        old_account = (host.hostname, host.username)
        new_account = (result["hostname"], result["username"])
        moved = new_account != old_account
        carried = get_password(*old_account) if moved else ""

        address_changed = new_account[0] != old_account[0] or result["port"] != host.port
        if address_changed and host is self._active:
            self._disconnect()

        host.hostname, host.username = new_account
        host.port = result["port"]
        host.label = result["label"]
        host.key_path = result["key_path"]

        # Keep the stored secret attached to the account it belongs to. Renaming
        # a host used to strand its password under the old user@host key, so the
        # next connect failed with no obvious cause.
        if moved and carried:
            delete_password(*old_account)
        secret = result["password"] or carried
        if secret:
            save_password(*new_account, secret)

        if new_account[0] != old_account[0]:
            # A different address is a different machine: what we learned about
            # the old one no longer applies.
            host.os_info = host.os_pretty = host.os_like = ""
            host.stacks = []
            host.pending_updates = UPDATES_UNKNOWN

        self._save_hosts_soon()
        self._host_list.set_hosts(self._hosts)
        self._host_list.set_active(self._active)
        if host is self._active:
            self._show_host(host)

    def _remove_host(self, host: Host) -> None:
        if not messagebox.askyesno(
            "Remove Host", f"Remove '{host.display_name}'?", parent=self
        ):
            return
        if host is self._active:
            self._disconnect()
            self._active = None
            self._clear_stacks()
            self._host_title.configure(text="Select a host")
            self._os_badge.configure(text="", fg_color=distro.DEFAULT_BRAND_COLOR)

        self._hosts.remove(host)
        # Credentials are keyed by user@host, which two entries may share.
        if not any(
            (h.hostname, h.username) == (host.hostname, host.username)
            for h in self._hosts
        ):
            delete_password(host.hostname, host.username)

        self._save_hosts_soon()
        self._host_list.set_hosts(self._hosts)
        self._host_list.set_active(self._active)

    def _ask_host(self, host: Optional[Host] = None) -> Optional[dict]:
        dialog = HostDialog(self, host=host)
        self.wait_window(dialog)
        return dialog.result

    def _import_from_ssh_config(self) -> None:
        """Bring in hosts from ``~/.ssh/config`` — VS Code Remote-SSH's own list.

        Reading the SSH config rather than any editor-specific store is what makes
        the two lists agree: Remote-SSH has no host list of its own.
        """
        paths = ssh_config.candidate_paths()
        config_hosts = ssh_config.load_hosts(paths)
        if not config_hosts:
            messagebox.showinfo(
                "Nothing to import",
                "No hosts found in:\n\n"
                + "\n".join(str(path) for path in paths)
                + "\n\nVS Code Remote-SSH reads ~/.ssh/config unless "
                  "remote.SSH.configFile points elsewhere.",
                parent=self,
            )
            return

        candidates = ssh_config.plan_import(config_hosts, self._hosts)
        dialog = ImportHostsDialog(self, candidates, paths)
        self.wait_window(dialog)
        if not dialog.result:
            return

        added, keyed = self._apply_import(dialog.result)
        self._save_hosts_soon()
        self._host_list.set_hosts(self._hosts)
        self._host_list.set_active(self._active)
        self._log.log(f"Import: {import_summary(dialog.result)}.", "success")
        if added:
            self._log.log(
                f"{added} imported host(s) use key authentication. Add a password "
                f"in Edit if a host also needs one for sudo.",
            )
        if keyed and self._active is not None and self._connected():
            self._log.log("Reconnect for an attached key to take effect.", "warn")

    def _apply_import(self, chosen: list[ssh_config.ImportCandidate]) -> tuple[int, int]:
        """Apply the chosen candidates. Returns ``(added, keys_attached)``."""
        added = keyed = 0
        for candidate in chosen:
            config_host = candidate.host
            if candidate.action == ssh_config.ACTION_ADD:
                self._hosts.append(
                    Host(
                        hostname=config_host.hostname,
                        username=config_host.user,
                        port=config_host.port,
                        label=config_host.alias,
                        key_path=config_host.identity_file,
                    )
                )
                added += 1
            elif candidate.action == ssh_config.ACTION_ATTACH_KEY:
                if 0 <= candidate.existing_index < len(self._hosts):
                    self._hosts[candidate.existing_index].key_path = (
                        config_host.identity_file
                    )
                    keyed += 1
        return added, keyed

    # ──────────────────────────────────────────────────────────────────────────
    # Selection and connection
    # ──────────────────────────────────────────────────────────────────────────

    def _select_host(self, host: Host) -> None:
        if self._busy or host is self._active:
            return
        self._disconnect()
        self._active = host
        self._host_list.set_active(host)
        self._show_host(host)
        self._btn_update_stacks.configure(state="disabled")
        self._populate_stacks(host.stacks)

    def _show_host(self, host: Host) -> None:
        self._host_title.configure(text=host.display_name)
        self._os_badge.configure(
            text=host.os_pretty or "Unknown OS", fg_color=os_badge_color(host)
        )

    def _toggle_connect(self) -> None:
        if self._active is None:
            return
        if self._connecting:
            self._cancel_connect()
        elif self._connected():
            name = self._active.display_name
            self._disconnect()
            self._log.log(f"Disconnected from {name}.")
        else:
            self._connect()

    def _connect(self) -> None:
        host = self._active
        if host is None:
            return

        ssh = self._client_for(host)
        if not host.key_path and not ssh.password:
            messagebox.showerror(
                "No credentials",
                f"No saved password or SSH key for {host.display_name}.\n"
                f"Edit the host to add one.",
                parent=self,
            )
            return

        # Deliberately not _set_busy(): a connection attempt must leave the rest
        # of the window usable. Locking it and showing a wait cursor is what made
        # an unreachable host look like the application had hung.
        self._connect_token += 1
        token = self._connect_token
        self._connecting = True
        self._btn_connect.configure(text="Cancel", state="normal")
        self._log.log(f"Connecting to {host.display_name} ({host.address})…")
        if host.key_path:
            self._log.log(f"Using key {host.key_path}")

        def task() -> None:
            ok, message = ssh.connect()
            self.after(0, lambda: self._on_connected(token, host, ssh, ok, message))

        _run_in_thread(task)

    def _cancel_connect(self) -> None:
        """Abandon the in-flight attempt.

        The worker thread cannot be interrupted, but it is now bounded by the
        reachability probe and will exit on its own; invalidating the token means
        its result is ignored and the session it may open is closed.
        """
        self._connect_token += 1
        self._connecting = False
        self._log.log("Connection attempt abandoned.", "warn")
        self._btn_connect.configure(text="Connect", state="normal")

    def _on_connected(
        self, token: int, host: Host, ssh: SSHClient, ok: bool, message: str
    ) -> None:
        if token != self._connect_token or host is not self._active:
            # Cancelled, or the selection moved on while we were connecting.
            _run_in_thread(ssh.disconnect)
            return

        self._connecting = False
        if not ok:
            self._log.log(f"Connection failed: {message}", "error")
            self._btn_connect.configure(text="Connect", state="normal")
            return

        self._ssh = ssh
        self._log.log(f"Connected to {host.display_name}.", "success")
        self._btn_connect.configure(text="Disconnect", state="normal")
        self._btn_scan.configure(state="normal")
        self._btn_sys_update.configure(state="normal")
        self._btn_maintenance.configure(state="normal")
        if host.stacks:
            self._btn_update_stacks.configure(state="normal")
        _run_in_thread(lambda: self._ensure_os_info(host, ssh))

    def _disconnect(self) -> None:
        # Invalidate any attempt still in flight so its result cannot revive a
        # session the user has already moved away from.
        self._connect_token += 1
        self._connecting = False
        # Secondary windows hold this session; they are useless once it closes.
        self._close_windows()
        if self._ssh is not None:
            self._ssh.disconnect()
            self._ssh = None
        self._btn_connect.configure(text="Connect", state="normal")
        self._btn_scan.configure(text="Scan Stacks", state="disabled")
        self._btn_sys_update.configure(state="disabled")
        self._btn_maintenance.configure(state="disabled")
        self._btn_update_stacks.configure(state="disabled")

    def _session(self) -> Optional[tuple[Host, SSHClient]]:
        """The selected host paired with its live session, or ``None``.

        Every action that talks to the selected host starts here, so the "are we
        still connected?" check and the unpacking happen in one place.
        """
        host, ssh = self._active, self._ssh
        if host is None or ssh is None or not ssh.is_connected:
            return None
        return host, ssh

    def _connected(self) -> bool:
        return self._session() is not None

    def _client_for(self, host: Host) -> SSHClient:
        return SSHClient(
            host.hostname,
            host.username,
            get_password(host.hostname, host.username),
            host.port,
            key_path=host.key_path,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # OS detection
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_os_info(self, host: Host, ssh: SSHClient) -> None:
        """Detect and remember the host's distro. Runs on a worker thread.

        The fields are assigned here rather than from the Tk callback because
        callers need them immediately to pick a package manager — deferring the
        assignment to ``after()`` meant a host's first update check always fell
        back to the default manager, whatever distro had just been detected.
        """
        if host.os_info or host.os_pretty:
            return
        info = ssh.detect_os()
        host.os_info, host.os_pretty, host.os_like = info.id, info.pretty, info.like
        self.after(0, lambda: self._on_os_info(host))

    def _on_os_info(self, host: Host) -> None:
        self._save_hosts_soon()
        self._log.log(f"{host.display_name} is running {host.os_pretty}.")
        if not distro.is_recognized(host.os_info, host.os_like):
            manager = self._package_manager(host)
            self._log.log(
                f"Unrecognised distribution — assuming {manager.name} for updates.",
                "warn",
            )
        if host is self._active:
            self._show_host(host)
        self._host_list.refresh()

    @staticmethod
    def _package_manager(host: Host) -> distro.PackageManager:
        return distro.resolve(host.os_info, host.os_like)

    # ──────────────────────────────────────────────────────────────────────────
    # Stacks
    # ──────────────────────────────────────────────────────────────────────────

    def _scan_stacks(self) -> None:
        session = self._session()
        if session is None:
            return
        host, ssh = session
        self._set_busy(True)
        self._btn_scan.configure(state="disabled", text="Scanning…")

        def task() -> None:
            stacks = ssh.find_docker_stacks(self._log.log)
            self.after(0, lambda: self._on_stacks_found(host, stacks))

        _run_in_thread(task)

    def _on_stacks_found(self, host: Host, stacks: list[DockerStack]) -> None:
        self._set_busy(False)
        self._btn_scan.configure(
            text="Scan Stacks", state="normal" if self._connected() else "disabled"
        )
        if host is not self._active:
            return  # Host was switched while the scan ran.

        host.stacks = stacks
        self._save_hosts_soon()
        self._populate_stacks(stacks)
        self._log.log(f"Found {len(stacks)} stack(s).", "success" if stacks else "warn")

    def _populate_stacks(self, stacks: list[DockerStack]) -> None:
        self._clear_stacks()
        if not stacks:
            ctk.CTkLabel(
                self._stack_scroll,
                text="No stacks found.\nConnect and scan to discover them.",
                text_color="gray60",
            ).pack(pady=20)
            return
        for stack in stacks:
            self._add_stack_row(stack)
        if self._connected():
            self._btn_update_stacks.configure(state="normal")

    def _clear_stacks(self) -> None:
        for widget in self._stack_scroll.winfo_children():
            widget.destroy()
        self._stack_vars.clear()

    def _add_stack_row(self, stack: DockerStack) -> None:
        selected = tk.BooleanVar(value=True)
        self._stack_vars[stack.path] = selected

        row = ctk.CTkFrame(self._stack_scroll, corner_radius=6)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(row, text="", variable=selected, width=24).grid(
            row=0, column=0, padx=(8, 2), pady=6
        )

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(
            info, text=stack.name, font=ctk.CTkFont(weight="bold"), anchor="w"
        ).pack(fill="x")
        ctk.CTkLabel(
            info,
            text=stack.path,
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x")

        ctk.CTkLabel(
            row,
            text=f"{STACK_ICONS.get(stack.status, '?')} {stack.status}",
            text_color=state_color(stack.status),
            font=ctk.CTkFont(size=12),
            width=86,
            anchor="w",
        ).grid(row=0, column=2, padx=(6, 2))

        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.grid(row=0, column=3, padx=(2, 8))
        ctk.CTkButton(
            buttons, text="Manage", width=68, height=26,
            command=lambda s=stack: self._open_stack(s),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            buttons, text="Edit", width=52, height=26,
            fg_color=ACCENT_TEAL, hover_color=ACCENT_TEAL_HOVER,
            command=lambda s=stack: self._edit_stack(s),
        ).pack(side="left", padx=2)

    def _select_all_stacks(self, selected: bool) -> None:
        for var in self._stack_vars.values():
            var.set(selected)

    # ──────────────────────────────────────────────────────────────────────────
    # Secondary windows
    # ──────────────────────────────────────────────────────────────────────────

    def _open_stack(self, stack: DockerStack) -> None:
        """Open (or re-focus) the control window for one stack."""
        session = self._session()
        if session is None:
            self._require_connection()
            return
        _host, ssh = session
        self._open_window(
            f"stack:{stack.path}",
            lambda: StackWindow(
                self,
                ssh,
                stack,
                on_changed=self._refresh_stack_status,
                on_edit=self._edit_stack,
            ),
        )

    def _edit_stack(self, stack: DockerStack) -> None:
        """Open (or re-focus) the compose editor for one stack."""
        session = self._session()
        if session is None:
            self._require_connection()
            return
        _host, ssh = session
        self._open_window(
            f"editor:{stack.compose_file}",
            lambda: ComposeEditor(
                self,
                ssh,
                stack,
                self._log.log,
                on_deployed=self._refresh_stack_status,
            ),
        )

    def _open_maintenance(self) -> None:
        session = self._session()
        if session is None:
            self._require_connection()
            return
        host, ssh = session
        self._open_window(
            "maintenance",
            lambda: MaintenanceWindow(self, ssh, host.display_name),
        )

    def _open_window(self, key: str, factory: Callable[[], ctk.CTkToplevel]) -> None:
        """Show a secondary window, re-using the existing one if it is still open.

        Without this, every click spawns another editor for the same file and two
        of them can save over each other.
        """
        existing = self._windows.get(key)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        window = factory()
        self._windows[key] = window

    def _close_windows(self) -> None:
        """Tear down secondary windows, e.g. when the session goes away."""
        for window in list(self._windows.values()):
            if window.winfo_exists():
                window.destroy()
        self._windows.clear()

    def _require_connection(self) -> None:
        messagebox.showinfo(
            "Not connected",
            "Connect to the host first.",
            parent=self,
        )

    def _refresh_stack_status(self) -> None:
        """Re-scan after a stack changed underneath us, if still connected."""
        if self._connected() and not self._busy:
            self._scan_stacks()

    def _selected_stacks(self) -> list[DockerStack]:
        if self._active is None:
            return []
        return [
            stack
            for stack in self._active.stacks
            if (var := self._stack_vars.get(stack.path)) is not None and var.get()
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # Updating the selected host
    # ──────────────────────────────────────────────────────────────────────────

    def _update_selected_stacks(self) -> None:
        session = self._session()
        if session is None:
            return
        host, ssh = session

        stacks = self._selected_stacks()
        if not stacks:
            messagebox.showinfo(
                "Nothing selected", "Select at least one stack to update.", parent=self
            )
            return
        if not messagebox.askyesno(
            "Confirm Update",
            f"Update {len(stacks)} stack(s) on '{host.display_name}'?\n\n"
            f"Images will be pulled and containers recreated.",
            parent=self,
        ):
            return

        self._set_busy(True)
        self._btn_update_stacks.configure(state="disabled", text="Updating…")
        self._log.log(f"Updating {len(stacks)} stack(s) on {host.display_name}…")

        def task() -> None:
            results = [ssh.update_stack(stack, self._log.log) for stack in stacks]
            self.after(0, lambda: self._on_stacks_updated(results))

        _run_in_thread(task)

    def _on_stacks_updated(self, results: list[bool]) -> None:
        self._set_busy(False)
        self._btn_update_stacks.configure(
            text="Update Selected Stacks",
            state="normal" if self._connected() else "disabled",
        )
        succeeded = sum(results)
        failed = len(results) - succeeded
        level = "success" if not failed else "warn" if succeeded else "error"
        self._log.log(
            f"Stack update finished: {succeeded} succeeded, {failed} failed.", level
        )
        if self._connected():
            self._scan_stacks()  # Refresh the status column.

    def _run_system_update(self) -> None:
        session = self._session()
        if session is None:
            return
        host, ssh = session
        manager = self._package_manager(host)
        if not messagebox.askyesno(
            "System Update",
            f"Run a full system package update on '{host.display_name}' "
            f"using {manager.name}?\n\nThis may take several minutes.",
            parent=self,
        ):
            return

        self._set_busy(True)
        self._btn_sys_update.configure(state="disabled", text="Updating…")

        def task() -> None:
            ok = ssh.run_system_update(manager, self._log.log)
            self.after(0, lambda: self._on_system_updated(host, ok))

        _run_in_thread(task)

    def _on_system_updated(self, host: Host, ok: bool) -> None:
        self._set_busy(False)
        self._btn_sys_update.configure(
            text="System Update", state="normal" if self._connected() else "disabled"
        )
        host.pending_updates = 0 if ok else UPDATES_FAILED
        self._log.log(
            f"{host.display_name}: system update "
            f"{'completed' if ok else 'failed'}.",
            "success" if ok else "error",
        )
        self._host_list.refresh()

    # ──────────────────────────────────────────────────────────────────────────
    # Bulk actions across every host
    # ──────────────────────────────────────────────────────────────────────────

    def _check_all_updates(self) -> None:
        def worker(host: Host, ssh: SSHClient, log: Logger) -> None:
            manager = self._package_manager(host)
            count = ssh.check_updates(manager)
            host.pending_updates = UPDATES_FAILED if count is None else count
            if count is None:
                log(f"could not read pending updates via {manager.name}", "warn")
            elif count == 0:
                log("up to date", "success")
            else:
                log(f"{count} update(s) available", "warn")

        self._for_each_host(
            "Update check", worker, button=self._btn_check_all, busy_text="Checking…"
        )

    def _update_all_hosts(self) -> None:
        def worker(host: Host, ssh: SSHClient, log: Logger) -> None:
            ok = ssh.run_system_update(self._package_manager(host), log)
            host.pending_updates = 0 if ok else UPDATES_FAILED

        self._for_each_host(
            "System update",
            worker,
            button=self._btn_update_all,
            busy_text="Updating…",
            confirm=(
                f"Run a full system package update on all {len(self._hosts)} host(s)?"
                f"\n\nThis may take several minutes per host."
            ),
        )

    def _for_each_host(
        self,
        action: str,
        worker: HostWorker,
        *,
        button: ctk.CTkButton,
        busy_text: str,
        confirm: Optional[str] = None,
    ) -> None:
        """Run ``worker(host, ssh, log)`` against every host, in parallel.

        Everything the bulk actions share lives here: confirming, disabling the
        buttons, connecting, one-time OS detection, prefixing log lines with the
        host name, disconnecting, and restoring the UI once the last host
        finishes. A worker only has to do the part that differs.
        """
        hosts = list(self._hosts)
        if not hosts:
            self._log.log("No hosts configured.", "warn")
            return
        if confirm and not messagebox.askyesno(action, confirm, parent=self):
            return

        self._set_bulk_enabled(False)
        button.configure(text=busy_text)
        self._log.log(f"{action}: starting on {len(hosts)} host(s)…")

        remaining = len(hosts)
        lock = threading.Lock()

        def run_one(host: Host) -> None:
            nonlocal remaining

            def log(message: str, level: str = "info") -> None:
                self._log.log(f"[{host.display_name}] {message}", level)

            try:
                with self._client_for(host) as ssh:
                    ok, error = ssh.connect()
                    if not ok:
                        host.pending_updates = UPDATES_FAILED
                        log(error, "error")
                    else:
                        self._ensure_os_info(host, ssh)
                        worker(host, ssh, log)
            except Exception as exc:  # A worker thread must not die silently.
                host.pending_updates = UPDATES_FAILED
                log(f"unexpected error: {exc}", "error")
            finally:
                with lock:
                    remaining -= 1
                    finished = remaining == 0
                self.after(0, self._host_list.refresh)
                if finished:
                    self.after(0, lambda: self._finish_bulk(action))

        pool = ThreadPoolExecutor(
            max_workers=min(MAX_PARALLEL_HOSTS, len(hosts)),
            thread_name_prefix="bulk-host",
        )
        for host in hosts:
            pool.submit(run_one, host)
        # Release the pool's threads as they drain; waiting here would block the
        # UI thread for the whole run.
        pool.shutdown(wait=False)

    def _finish_bulk(self, action: str) -> None:
        self._set_bulk_enabled(True)
        self._save_hosts_soon()
        self._host_list.refresh()
        self._log.log(f"{action}: finished on all hosts.", "success")

    def _set_bulk_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._btn_check_all.configure(state=state, text="Check All Updates")
        self._btn_update_all.configure(state=state, text="Update All Hosts")

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.configure(cursor="watch" if busy else "")

    def _save_hosts_soon(self) -> None:
        """Persist the host list without blocking the window.

        The snapshot is taken here, on the UI thread, so it cannot tear; only the
        disk write is deferred. Bursts — a bulk update check finishing on nine
        hosts at once — collapse into a single write.
        """
        save_hosts_async(self._hosts, on_error=self._on_save_failed)

    def _on_save_failed(self, error: Exception) -> None:
        # Called from the writer thread; the log panel is safe to use from there.
        self._log.log(f"Could not save host configuration: {error}", "error")

    def _on_close(self) -> None:
        self._disconnect()
        try:
            # Synchronous on the way out, then wait for anything the background
            # writer still holds: a deferred write must not die with the process.
            save_hosts(self._hosts)
            flush_pending_saves()
        except OSError:
            pass  # Nothing useful to show; the window is already closing.
        self.destroy()


def _read_hosts() -> tuple[list[Host], int]:
    """Load hosts from disk, returning the valid ones and how many were skipped.

    One malformed record should cost the user that record, not the application.
    """
    hosts: list[Host] = []
    skipped = 0
    for entry in load_hosts():
        try:
            hosts.append(Host.from_dict(entry))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    return hosts, skipped


def _run_in_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()
