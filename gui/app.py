"""Main application window.

The window owns the host list, one optional SSH session to the *selected* host,
and the output log. Anything that touches the network runs on a worker thread
and comes back through :meth:`tkinter.Misc.after`; log lines are the exception,
since :class:`~gui.log_panel.LogPanel` is itself thread-safe.

Bulk actions ("all hosts") each open their own short-lived connection, so they
are independent of whatever the selected host is doing.
"""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

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
from gui import dialogs, settings, theme
from gui.compose_editor import ComposeEditor
from gui.host_dialog import HostDialog
from gui.host_list import HostList, os_badge_color
from gui.import_dialog import ImportHostsDialog
from gui.import_dialog import summarize as import_summary
from gui.log_panel import LogPanel
from gui.maintenance import MaintenanceWindow
from gui.palette import Command, CommandPalette
from gui.stack_window import StackWindow
from models.host import (
    UPDATES_FAILED,
    UPDATES_UNKNOWN,
    DockerStack,
    Host,
    merge_stacks,
)

MAX_PARALLEL_HOSTS = 8
"""Cap on concurrent SSH sessions during a bulk action.

One thread per host stops being an optimisation somewhere around a dozen hosts;
past that it is just contention and a fan of simultaneous auth attempts.
"""

APP_TITLE = "Docker Stack Manager"

#: Order and wording of the stack-state summary chips above the list.
_STATE_ORDER = ("running", "partial", "stopped", "unknown")

Logger = Callable[..., None]
HostWorker = Callable[[Host, SSHClient, Logger], None]


class App(ctk.CTk):
    """The application window."""

    def __init__(self):
        theme.apply()
        theme.set_app_id()
        super().__init__()
        theme.init_fonts(self)

        self.title(APP_TITLE)
        self.geometry("1320x820")
        self.minsize(1120, 660)
        theme.style_window(self)

        self._hosts, skipped = _read_hosts()
        self._active: Optional[Host] = None
        self._ssh: Optional[SSHClient] = None
        self._stack_vars: dict[str, tk.BooleanVar] = {}
        self._windows: dict[str, ctk.CTkToplevel] = {}
        self._busy = False
        self._bulk_running = False
        self._connecting = False
        self._connect_token = 0
        """Incremented to abandon an in-flight connection attempt."""

        self._build_ui()
        self._bind_shortcuts()
        self._refresh_host_list()
        self._show_no_host()
        self._restore_window_state()
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
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        theme.divider(self, vertical=True).grid(row=0, column=1, sticky="ns")

        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.grid(row=0, column=2, sticky="nsew")
        content.grid_rowconfigure(2, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self._build_header(content)

        # A hairline progress bar between header and body. It is always laid out
        # so the body never jumps; idle, it is painted in the background colour.
        self._progress = ctk.CTkProgressBar(
            content, mode="indeterminate", height=2, corner_radius=0,
            fg_color=theme.BG, progress_color=theme.BG,
        )
        self._progress.grid(row=1, column=0, sticky="ew", padx=24)

        split = ctk.CTkFrame(content, fg_color="transparent")
        split.grid(row=2, column=0, sticky="nsew", padx=24, pady=(14, 24))
        split.grid_rowconfigure(0, weight=1)
        split.grid_columnconfigure((0, 1), weight=1, uniform="split")

        self._build_stack_panel(split)

        self._log = LogPanel(split, title="Activity")
        self._log.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=272, corner_radius=0, fg_color=theme.SIDEBAR)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(3, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 18))
        self._logo = _load_logo(self, 30)
        if self._logo is not None:
            tk.Label(brand, image=self._logo, bg=theme.SIDEBAR, bd=0).grid(
                row=0, column=0, rowspan=2, padx=(0, 11)
            )
        theme.heading(brand, "Stack Manager", role="heading").grid(row=0, column=1, sticky="sw")
        theme.caption(brand, "Docker & system updates").grid(row=1, column=1, sticky="nw")

        # A search field that opens the command palette — the discoverable half
        # of Ctrl+K.
        search = ctk.CTkButton(
            sidebar, text="       Search or run a command", anchor="w", height=34,
            corner_radius=8, fg_color=theme.INSET, hover_color=theme.SURFACE_2,
            border_width=1, border_color=theme.BORDER, text_color=theme.TEXT_3,
            font=theme.font("small"), command=self._open_palette,
        )
        search.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 18))
        shortcut = theme.text(search, "Ctrl K", "caption", theme.TEXT_3,
                              fg_color=theme.SURFACE_2, corner_radius=5, width=46,
                              anchor="center")
        shortcut.place(relx=1.0, rely=0.5, x=-8, anchor="e")
        glass = theme.search_icon(search, size=14, bg=theme.INSET)
        glass.place(x=12, rely=0.5, anchor="w")
        for overlay in (shortcut, glass):
            theme.bind_click(overlay, self._open_palette)

        section = ctk.CTkFrame(sidebar, fg_color="transparent")
        section.grid(row=2, column=0, sticky="ew", padx=(20, 12), pady=(0, 6))
        section.grid_columnconfigure(0, weight=1)
        theme.overline(section, "Hosts").grid(row=0, column=0, sticky="w")
        self._host_count = theme.caption(section, "", color=theme.TEXT_3)
        self._host_count.grid(row=0, column=1, sticky="e", padx=(0, 6))
        add_icon = theme.icon_button(section, "+", self._add_host, size=24,
                                     font=theme.font("title"))
        add_icon.grid(row=0, column=2, sticky="e")
        theme.Tooltip(add_icon, "Add host  (Ctrl+N)")

        self._host_list = HostList(
            sidebar,
            on_select=self._select_host,
            on_edit=self._edit_host,
            on_remove=self._remove_host,
            on_toggle=self._on_bulk_toggled,
            on_activate=self._activate_host,
        )
        # The list keeps its own gutter on the trailing edge for the scrollbar.
        self._host_list.grid(row=3, column=0, sticky="nsew", padx=(8, 0))

        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 16))
        footer.grid_columnconfigure((0, 1), weight=1, uniform="footer")

        theme.button(footer, "Add host", self._add_host, variant="secondary",
                     size="sm").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        import_button = theme.button(footer, "Import…", self._import_from_ssh_config,
                                     variant="secondary", size="sm")
        import_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        theme.Tooltip(import_button, "Import hosts from ~/.ssh/config (Ctrl+I)")

        theme.divider(footer).grid(row=1, column=0, columnspan=2, sticky="ew", pady=14)
        theme.overline(footer, "All hosts").grid(row=2, column=0, sticky="w",
                                                pady=(0, 8), padx=4)
        self._bulk_scope = theme.caption(footer, "", role="caption", anchor="e")
        self._bulk_scope.grid(row=2, column=1, sticky="e", pady=(0, 8), padx=4)
        theme.Tooltip(self._bulk_scope, "Use the switch beside each host to include or skip it")

        self._btn_check_all = theme.button(
            footer, "Check for updates", self._check_all_updates,
            variant="secondary", size="sm",
        )
        self._btn_check_all.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._btn_update_all = theme.button(
            footer, "Update all hosts", self._update_all_hosts,
            variant="secondary", size="sm",
        )
        self._btn_update_all.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_header(self, parent) -> None:
        bar = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 12))
        bar.grid_columnconfigure(0, weight=1)

        identity = ctk.CTkFrame(bar, fg_color="transparent")
        identity.grid(row=0, column=0, sticky="w")

        title_row = ctk.CTkFrame(identity, fg_color="transparent")
        title_row.pack(anchor="w")
        self._host_title = theme.heading(title_row, "No host selected", role="display")
        self._host_title.pack(side="left")

        # OS chip: a dot in the distro's brand colour beside its name.
        self._os_chip = ctk.CTkFrame(
            title_row, fg_color=theme.SURFACE_2, corner_radius=11, height=24,
            border_width=1, border_color=theme.BORDER,
        )
        self._os_dot = ctk.CTkLabel(self._os_chip, text="●", font=theme.font("caption"), width=8)
        self._os_dot.pack(side="left", padx=(10, 6))
        self._os_badge = ctk.CTkLabel(
            self._os_chip, text="", font=theme.font("small"), text_color=theme.TEXT_2, height=22
        )
        self._os_badge.pack(side="left", padx=(0, 11))

        meta = ctk.CTkFrame(identity, fg_color="transparent")
        meta.pack(anchor="w", pady=(6, 0))
        self._conn_pill = theme.pill(meta, "", theme.TEXT_3, base=theme.BG)
        self._conn_pill.pack(side="left")
        self._host_meta = theme.caption(meta, "", color=theme.TEXT_3, role="small")
        self._host_meta.pack(side="left", padx=(10, 0))

        # What is running right now, in words, beside a spinner.
        self._activity = ctk.CTkFrame(meta, fg_color="transparent")
        self._spinner = theme.Spinner(self._activity, size=13, bg=theme.BG)
        self._spinner.pack(side="left", padx=(0, 7))
        self._activity_text = theme.caption(self._activity, "", color=theme.ACCENT_TEXT,
                                            role="small_strong")
        self._activity_text.pack(side="left")

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e")

        self._btn_maintenance = theme.button(
            actions, "Cleanup", self._open_maintenance, state="disabled"
        )
        self._btn_maintenance.pack(side="left", padx=(0, 8))
        theme.Tooltip(self._btn_maintenance, "Disk usage and Docker pruning")

        self._btn_sys_update = theme.button(
            actions, "System update", self._run_system_update, width=128, state="disabled"
        )
        self._btn_sys_update.pack(side="left", padx=(0, 8))
        theme.Tooltip(self._btn_sys_update, "Upgrade the host's OS packages")

        self._btn_scan = theme.button(
            actions, "Scan stacks", self._scan_stacks, width=116, state="disabled"
        )
        self._btn_scan.pack(side="left", padx=(0, 8))
        theme.Tooltip(self._btn_scan, "Discover Compose projects  (F5)")

        self._btn_connect = theme.button(
            actions, "Connect", self._toggle_connect, variant="primary", width=118,
            state="disabled",
        )
        self._btn_connect.pack(side="left")

    def _build_stack_panel(self, parent) -> None:
        panel = theme.card(parent)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        panel.grid_rowconfigure(2, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=(16, 10), pady=(12, 0))
        header.grid_columnconfigure(1, weight=1)
        theme.heading(header, "Stacks", role="heading").grid(row=0, column=0, sticky="w")
        self._stack_count = theme.caption(header, "", color=theme.TEXT_3, role="small")
        self._stack_count.grid(row=0, column=1, sticky="w", padx=(8, 0))

        toggles = ctk.CTkFrame(header, fg_color="transparent")
        toggles.grid(row=0, column=2, sticky="e")
        theme.button(toggles, "Select all", lambda: self._select_all_stacks(True),
                     variant="ghost", size="sm").pack(side="left")
        theme.button(toggles, "None", lambda: self._select_all_stacks(False),
                     variant="ghost", size="sm").pack(side="left", padx=(2, 0))

        self._stack_summary = ctk.CTkFrame(panel, fg_color="transparent", height=0)
        self._stack_summary.grid(row=1, column=0, sticky="ew", padx=16, pady=(6, 8))

        self._stack_scroll = theme.scrollable(panel)
        self._stack_scroll.grid(row=2, column=0, sticky="nsew", padx=(8, 4))
        self._stack_scroll.grid_columnconfigure(0, weight=1)

        theme.divider(panel).grid(row=3, column=0, sticky="ew", padx=1)
        footer = ctk.CTkFrame(panel, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=16, pady=12)
        footer.grid_columnconfigure(0, weight=1)
        note = "Pulls new images and recreates containers."
        hint = theme.caption(footer, note, color=theme.TEXT_3)
        hint.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        theme.elide(hint, note, "caption")
        self._btn_update_stacks = theme.button(
            footer, "Update selected", self._update_selected_stacks,
            variant="primary", width=156, state="disabled",
        )
        self._btn_update_stacks.grid(row=0, column=1, sticky="e")

    def _bind_shortcuts(self) -> None:
        for sequence, handler in (
            ("<Control-n>", self._add_host),
            ("<Control-i>", self._import_from_ssh_config),
            ("<F5>", self._scan_stacks),
            ("<Control-l>", lambda: self._log.clear()),
            ("<Control-k>", self._open_palette),
            ("<Control-p>", self._open_palette),
        ):
            self.bind(sequence, lambda _event, h=handler: h())

    # ──────────────────────────────────────────────────────────────────────────
    # Command palette
    # ──────────────────────────────────────────────────────────────────────────

    def _open_palette(self) -> None:
        existing = getattr(self, "_palette", None)
        if existing is not None and existing.winfo_exists():
            existing.close()
            return
        self._palette = CommandPalette(self, self._palette_commands())

    def _palette_commands(self) -> list[Command]:
        """Everything that can be done right now, as palette entries."""
        commands: list[Command] = []

        def add(title, action, subtitle="", group="", hint="", keywords=""):
            commands.append(Command(title, action, subtitle, group, hint, keywords,
                                    order=len(commands)))

        connected = self._connected()
        active = self._active
        if active is not None and connected:
            for stack in active.stacks:
                add(f"Manage {stack.name}", lambda s=stack: self._open_stack(s),
                    stack.path, "Stack", keywords=f"containers logs {stack.status}")
            for stack in active.stacks:
                add(f"Edit {stack.name} compose file", lambda s=stack: self._edit_stack(s),
                    stack.compose_file, "Stack", keywords="yaml editor")
        if active is not None and not self._busy:
            if self._connecting:
                add("Cancel connecting", self._toggle_connect, active.display_name, "Host")
            elif connected:
                add("Scan stacks", self._scan_stacks, active.display_name, "Host", "F5")
                add("System update", self._run_system_update, active.display_name, "Host",
                    keywords="upgrade packages apt dnf")
                add("Cleanup and disk usage", self._open_maintenance, active.display_name,
                    "Host", keywords="prune docker df space")
                add(f"Disconnect from {active.display_name}", self._toggle_connect,
                    active.address, "Host")
            else:
                add(f"Connect to {active.display_name}", self._toggle_connect,
                    active.address, "Host")
        for host in self._hosts:
            if host is not active:
                add(f"Go to {host.display_name}", lambda h=host: self._select_host(h),
                    f"{host.address}   {host.os_pretty}".strip(), "Host",
                    keywords="switch select")
        if active is not None:
            add(f"Edit {active.display_name}…", lambda h=active: self._edit_host(h),
                active.address, "Host", keywords="settings password key port")
        if self._bulk_hosts() and self._btn_check_all.cget("state") == "normal":
            scope = f"{len(self._bulk_hosts())} of {len(self._hosts)} hosts included"
            add("Check all hosts for updates", self._check_all_updates, scope, "All hosts")
            add("Update all hosts", self._update_all_hosts, scope, "All hosts",
                keywords="upgrade packages")
        for host in self._hosts:
            verb = "Skip" if host.include_in_bulk else "Include"
            add(f"{verb} {host.display_name} in fleet-wide updates",
                lambda h=host: self._set_included(h, not h.include_in_bulk),
                host.address, "Host", keywords="toggle bulk all hosts exclude")
        add("Add host…", self._add_host, group="App", hint="Ctrl N", keywords="new")
        add("Import hosts from SSH config…", self._import_from_ssh_config, group="App",
            hint="Ctrl I", keywords="vscode remote")
        add("Save activity log…", self._log.save, group="App", keywords="export")
        add("Clear activity log", self._log.clear, group="App", hint="Ctrl L")
        return commands

    # ──────────────────────────────────────────────────────────────────────────
    # Header and empty states
    # ──────────────────────────────────────────────────────────────────────────

    def _show_no_host(self) -> None:
        """Header and stack panel for when nothing is selected."""
        self._host_title.configure(text="No host selected")
        self._os_chip.pack_forget()
        self._host_meta.configure(text="")
        theme.set_pill(self._conn_pill, "", theme.TEXT_3, base=theme.BG)
        self._conn_pill.pack_forget()
        self._btn_connect.configure(state="disabled")
        self._host_count.configure(text=str(len(self._hosts)) if self._hosts else "")
        self._clear_stacks()
        if self._hosts:
            self._empty_state(
                "Pick a host to begin",
                "Choose one from the sidebar to see its stacks.",
            )
        else:
            self._empty_state(
                "Add your first host",
                "Point the app at a Linux machine you reach over SSH,\n"
                "or import the hosts VS Code Remote-SSH already knows.",
                actions=(("Add host", self._add_host, "primary"),
                         ("Import from SSH config", self._import_from_ssh_config, "secondary")),
            )

    def _empty_state(self, title: str, message: str, actions=()) -> None:
        box = ctk.CTkFrame(self._stack_scroll, fg_color="transparent")
        box.pack(fill="x", pady=(70, 20))
        ctk.CTkLabel(box, text="◇", font=ctk.CTkFont(size=30), text_color=theme.BORDER_STRONG).pack()
        theme.heading(box, title, role="title").pack(pady=(8, 4))
        ctk.CTkLabel(
            box, text=message, font=theme.font("small"), text_color=theme.TEXT_3, justify="center"
        ).pack()
        if actions:
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(pady=(16, 0))
            for label, command, variant in actions:
                theme.button(row, label, command, variant=variant).pack(side="left", padx=4)

    def _update_connection_display(self) -> None:
        """Connection pill and host caption, from the current state."""
        host = self._active
        if host is None:
            return
        if self._connecting:
            text, color = "Connecting…", theme.WARNING
        elif self._connected():
            text, color = "Connected", theme.SUCCESS
        else:
            text, color = "Not connected", theme.TEXT_3
        theme.set_pill(self._conn_pill, text, color, base=theme.BG)
        self._conn_pill.pack(side="left", before=self._host_meta)

        bits = [host.address]
        if host.key_path:
            bits.append("key auth")
        if host.pending_updates > 0:
            bits.append(f"{host.pending_updates} OS update{'s' if host.pending_updates != 1 else ''} pending")
        self._host_meta.configure(text="   ·   ".join(bits))
        self._host_list.set_connected(host if self._connected() else None)

    def _set_connect_button(self, mode: str) -> None:
        """``connect``, ``cancel`` or ``disconnect``: label and emphasis together."""
        label, variant = {
            "connect": ("Connect", "primary"),
            "cancel": ("Cancel", "secondary"),
            "disconnect": ("Disconnect", "secondary"),
        }[mode]
        self._btn_connect.configure(text=label, state="normal", **theme.variant_colors(variant))

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
        self._refresh_host_list()
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
        self._refresh_host_list()
        if host is self._active:
            self._show_host(host)

    def _remove_host(self, host: Host) -> None:
        if not dialogs.askyesno(
            "Remove host?",
            f"{host.display_name} ({host.address}) will be removed from the list, along "
            f"with its saved password. Nothing on the host itself changes.",
            parent=self, confirm="Remove host", tone="danger",
        ):
            return
        was_active = host is self._active
        if was_active:
            self._disconnect()
            self._active = None

        self._hosts.remove(host)
        # Credentials are keyed by user@host, which two entries may share.
        if not any(
            (h.hostname, h.username) == (host.hostname, host.username)
            for h in self._hosts
        ):
            delete_password(host.hostname, host.username)

        self._save_hosts_soon()
        self._refresh_host_list()
        if was_active:
            self._show_no_host()

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
            dialogs.showinfo(
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
        self._refresh_host_list()
        if self._active is None:
            self._show_no_host()
        self._log.log(f"Import: {import_summary(dialog.result)}.", "success")
        dialogs.toast(self, "Import complete", detail=import_summary(dialog.result).capitalize())
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
        if host is self._active:
            return
        if self._busy:
            # Say so. Silently ignoring the click made the sidebar look broken
            # for however long a scan or an update happened to be running.
            self._log.log(
                f"Busy — finish or wait for the current operation before "
                f"switching to {host.display_name}.",
                "warn",
            )
            return
        self._disconnect()
        self._active = host
        self._host_list.set_active(host)
        self._show_host(host)
        self._btn_update_stacks.configure(state="disabled")
        self._populate_stacks(host.stacks)

    def _activate_host(self, host: Host) -> None:
        """Double-click in the sidebar: select the host and connect to it."""
        self._select_host(host)
        if (host is self._active and not self._busy and not self._connecting
                and not self._connected()):
            self._connect()

    def _show_host(self, host: Host) -> None:
        self._host_title.configure(text=host.display_name)
        self._os_badge.configure(text=host.os_pretty or "OS not detected yet")
        self._os_dot.configure(
            text_color=theme.lighten(os_badge_color(host), 0.25)
            if host.os_pretty else theme.TEXT_3
        )
        self._os_chip.pack(side="left", padx=(14, 0), pady=(4, 0))
        if not self._connecting and not self._connected():
            self._set_connect_button("connect")
        self._update_connection_display()

    def _refresh_host_list(self) -> None:
        self._host_list.set_hosts(self._hosts)
        self._host_list.set_active(self._active)
        self._host_count.configure(text=str(len(self._hosts)) if self._hosts else "")
        self._update_bulk_scope()

    def _bulk_hosts(self) -> list[Host]:
        """The hosts the fleet-wide actions act on: those switched on in the sidebar."""
        return [host for host in self._hosts if host.include_in_bulk]

    def _set_included(self, host: Host, included: bool) -> None:
        host.include_in_bulk = included
        self._host_list.refresh()
        self._on_bulk_toggled(host)

    def _on_bulk_toggled(self, host: Host) -> None:
        self._save_hosts_soon()
        self._update_bulk_scope()
        state = "included in" if host.include_in_bulk else "skipped by"
        self._log.log(f"{host.display_name} is now {state} fleet-wide updates.")

    def _update_bulk_scope(self) -> None:
        """"4 of 5 included" beside the section, and the buttons' availability."""
        total, included = len(self._hosts), len(self._bulk_hosts())
        if not total:
            text = ""
        elif included == total:
            text = f"All {total}" if total > 1 else "1 host"
        else:
            text = f"{included} of {total} included"
        self._bulk_scope.configure(
            text=text, text_color=theme.TEXT_3 if included == total else theme.WARNING
        )
        if not getattr(self, "_bulk_running", False):
            self._set_bulk_enabled(True)

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
            dialogs.showerror(
                "No credentials",
                f"There is no saved password or SSH key for {host.display_name}. "
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
        self._set_connect_button("cancel")
        self._update_connection_display()
        self._show_progress(True)
        self._log.log(f"Connecting to {host.display_name} ({host.address})…")
        if host.key_path:
            self._log.log(f"Using key {host.key_path}")

        def task() -> None:
            ok, message = ssh.connect()
            self.after(0, lambda: self._on_connected(token, host, ssh, ok, message))

        theme.run_in_thread(task)

    def _cancel_connect(self) -> None:
        """Abandon the in-flight attempt.

        The worker thread cannot be interrupted, but it is now bounded by the
        reachability probe and will exit on its own; invalidating the token means
        its result is ignored and the session it may open is closed.
        """
        self._connect_token += 1
        self._connecting = False
        self._log.log("Connection attempt abandoned.", "warn")
        self._set_connect_button("connect")
        self._show_progress(self._busy)
        self._update_connection_display()

    def _on_connected(
        self, token: int, host: Host, ssh: SSHClient, ok: bool, message: str
    ) -> None:
        if token != self._connect_token or host is not self._active:
            # Cancelled, or the selection moved on while we were connecting.
            theme.run_in_thread(ssh.disconnect)
            return

        self._connecting = False
        self._show_progress(self._busy)
        if not ok:
            self._log.log(f"Connection failed: {message}", "error")
            dialogs.toast(self, f"Could not connect to {host.display_name}", "error",
                          detail=_first_sentence(message))
            self._set_connect_button("connect")
            self._update_connection_display()
            return

        self._ssh = ssh
        self._log.log(f"Connected to {host.display_name}.", "success")
        dialogs.toast(self, f"Connected to {host.display_name}")
        self._set_connect_button("disconnect")
        self._update_connection_display()
        if not host.stacks:
            self._populate_stacks([])  # Swap the empty state's prompt to "scan".
        self._btn_scan.configure(state="normal")
        self._btn_sys_update.configure(state="normal")
        self._btn_maintenance.configure(state="normal")
        self._update_selection_label()
        theme.run_in_thread(lambda: self._ensure_os_info(host, ssh))

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
        self._show_progress(self._busy)
        if self._active is not None:
            self._set_connect_button("connect")
            self._update_connection_display()
        self._btn_scan.configure(text="Scan stacks", state="disabled")
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
        self._set_busy(True, "Scanning for stacks…")
        self._btn_scan.configure(state="disabled", text="Scanning…")

        def task() -> None:
            stacks = ssh.find_docker_stacks(self._log.log)
            self.after(0, lambda: self._on_stacks_found(host, stacks))

        theme.run_in_thread(task)

    def _on_stacks_found(self, host: Host, stacks: list[DockerStack]) -> None:
        self._set_busy(False)
        self._btn_scan.configure(
            text="Scan stacks", state="normal" if self._connected() else "disabled"
        )
        if host is not self._active:
            return  # Host was switched while the scan ran.

        # Carry rollback points across the rescan. A scan only reports what is on
        # the host, and the recorded pre-update image versions are ours alone.
        host.stacks = merge_stacks(host.stacks, stacks)
        self._save_hosts_soon()
        self._populate_stacks(stacks)
        self._log.log(f"Found {len(stacks)} stack(s).", "success" if stacks else "warn")
        dialogs.toast(self, f"Found {len(stacks)} stack{'' if len(stacks) == 1 else 's'}",
                      "success" if stacks else "warning")

    def _populate_stacks(self, stacks: list[DockerStack]) -> None:
        self._clear_stacks()
        self._show_stack_summary(stacks)
        if not stacks:
            if self._connected():
                self._empty_state(
                    "No stacks found",
                    "Scan the host to discover its Compose projects —\n"
                    "running or not.",
                    actions=(("Scan stacks", self._scan_stacks, "primary"),),
                )
            else:
                self._empty_state(
                    "Nothing discovered yet",
                    "Connect, then scan to find this host's Compose projects.",
                    actions=(("Connect", self._toggle_connect, "primary"),),
                )
            return
        for stack in stacks:
            self._add_stack_row(stack)
        self._update_selection_label()

    def _clear_stacks(self) -> None:
        for widget in self._stack_scroll.winfo_children():
            widget.destroy()
        self._stack_vars.clear()
        self._show_stack_summary([])
        self._update_selection_label()

    def _show_stack_summary(self, stacks: list[DockerStack]) -> None:
        """Count chips — ``4 running``, ``1 partial``… — above the list."""
        for widget in self._stack_summary.winfo_children():
            widget.destroy()
        self._stack_count.configure(text=f"{len(stacks)}" if stacks else "")
        counts: dict[str, int] = {}
        for stack in stacks:
            key = stack.status if stack.status in _STATE_ORDER else "unknown"
            counts[key] = counts.get(key, 0) + 1
        for state in _STATE_ORDER:
            if counts.get(state):
                theme.pill(
                    self._stack_summary, f"{counts[state]} {state}", theme.state_color(state),
                    base=theme.SURFACE,
                ).pack(side="left", padx=(0, 6))
        if not counts:
            self._stack_summary.configure(height=1)

    def _add_stack_row(self, stack: DockerStack) -> None:
        selected = tk.BooleanVar(value=True)
        selected.trace_add("write", lambda *_: self._update_selection_label())
        self._stack_vars[stack.path] = selected

        row = ctk.CTkFrame(
            self._stack_scroll, corner_radius=10, fg_color=theme.SURFACE_2,
            border_width=1, border_color=theme.BORDER,
        )
        row.pack(fill="x", pady=3, padx=(0, 4))
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(
            row, text="", variable=selected, width=20, checkbox_width=18,
            checkbox_height=18, border_width=2,
        ).grid(row=0, column=0, padx=(14, 4))

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", padx=(4, 8), pady=10)
        name = theme.text(info, stack.name, "body_strong")
        name.pack(fill="x")
        theme.elide(name, stack.name, "body_strong")
        path = theme.text(info, stack.path, "mono_small", theme.TEXT_3)
        path.pack(fill="x", pady=(1, 0))
        theme.elide(path, stack.path, "mono_small")

        theme.pill(row, stack.status, theme.state_color(stack.status), width=88).grid(
            row=0, column=2, padx=(0, 10)
        )

        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.grid(row=0, column=3, padx=(0, 10))
        manage = theme.button(buttons, "Manage", lambda s=stack: self._open_stack(s),
                              variant="secondary", size="sm")
        manage.pack(side="left", padx=(0, 4))
        edit = theme.icon_button(buttons, "edit", lambda s=stack: self._edit_stack(s))
        edit.pack(side="left")
        theme.Tooltip(manage, "Containers, lifecycle and logs")
        theme.Tooltip(edit, "Edit the compose file")

        theme.hover_row(row)

    def _update_selection_label(self) -> None:
        """Keep the update button honest about how many stacks it will touch."""
        if self._busy:
            return
        count = sum(1 for var in self._stack_vars.values() if var.get())
        enabled = count > 0 and self._connected()
        if not self._stack_vars:
            label = "Update selected"
        else:
            label = f"Update {count} stack{'' if count == 1 else 's'}" if count else "None selected"
        self._btn_update_stacks.configure(text=label, state="normal" if enabled else "disabled")

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
        dialogs.toast(self, "Not connected", "warning", detail="Connect to the host first.")

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
            dialogs.toast(self, "Nothing selected", "warning",
                          detail="Tick at least one stack to update.")
            return
        names = ", ".join(stack.name for stack in stacks[:6])
        if len(stacks) > 6:
            names += f" and {len(stacks) - 6} more"
        plural = "" if len(stacks) == 1 else "s"
        if not dialogs.askyesno(
            f"Update {len(stacks)} stack{plural}?",
            f"New images will be pulled on {host.display_name} and the containers of "
            f"{names} recreated. Running services restart briefly.",
            parent=self, confirm=f"Update {len(stacks)} stack{plural}",
        ):
            return

        self._set_busy(True, f"Updating {len(stacks)} stack{'' if len(stacks) == 1 else 's'}…")
        self._btn_update_stacks.configure(state="disabled", text="Updating…")
        self._log.log(f"Updating {len(stacks)} stack(s) on {host.display_name}…")

        def task() -> None:
            results = [ssh.update_stack(stack, self._log.log) for stack in stacks]
            self.after(0, lambda: self._on_stacks_updated(results))

        theme.run_in_thread(task)

    def _on_stacks_updated(self, results: list[bool]) -> None:
        self._set_busy(False)
        self._update_selection_label()
        succeeded = sum(results)
        failed = len(results) - succeeded
        level = "success" if not failed else "warn" if succeeded else "error"
        self._log.log(
            f"Stack update finished: {succeeded} succeeded, {failed} failed.", level
        )
        dialogs.toast(
            self, "Stacks updated" if not failed else f"{failed} stack update(s) failed",
            "success" if not failed else "error" if not succeeded else "warning",
            detail=f"{succeeded} succeeded, {failed} failed",
        )
        if self._connected():
            self._scan_stacks()  # Refresh the status column.

    def _run_system_update(self) -> None:
        session = self._session()
        if session is None:
            return
        host, ssh = session
        manager = self._package_manager(host)
        if not dialogs.askyesno(
            "Run a system update?",
            f"Every OS package on {host.display_name} will be upgraded with "
            f"{manager.name}. This can take several minutes; output streams to the "
            f"activity log.",
            parent=self, confirm="Update packages",
        ):
            return

        self._set_busy(True, f"Upgrading packages with {manager.name}…")
        self._btn_sys_update.configure(state="disabled", text="Updating…")

        def task() -> None:
            ok = ssh.run_system_update(manager, self._log.log)
            self.after(0, lambda: self._on_system_updated(host, ok))

        theme.run_in_thread(task)

    def _on_system_updated(self, host: Host, ok: bool) -> None:
        self._set_busy(False)
        self._btn_sys_update.configure(
            text="System update", state="normal" if self._connected() else "disabled"
        )
        host.pending_updates = 0 if ok else UPDATES_FAILED
        self._log.log(
            f"{host.display_name}: system update "
            f"{'completed' if ok else 'failed'}.",
            "success" if ok else "error",
        )
        dialogs.toast(self, "System update complete" if ok else "System update failed",
                      "success" if ok else "error", detail=host.display_name)
        self._host_list.refresh()
        if host is self._active:
            self._update_connection_display()

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
            confirm=_bulk_update_prompt(self._bulk_hosts(), len(self._hosts)),
            confirm_label=f"Update {len(self._bulk_hosts())} "
                          f"host{'' if len(self._bulk_hosts()) == 1 else 's'}",
        )

    def _for_each_host(
        self,
        action: str,
        worker: HostWorker,
        *,
        button: ctk.CTkButton,
        busy_text: str,
        confirm: Optional[str] = None,
        confirm_label: str = "",
    ) -> None:
        """Run ``worker(host, ssh, log)`` against every host, in parallel.

        Everything the bulk actions share lives here: confirming, disabling the
        buttons, connecting, one-time OS detection, prefixing log lines with the
        host name, disconnecting, and restoring the UI once the last host
        finishes. A worker only has to do the part that differs.
        """
        hosts = self._bulk_hosts()
        if not hosts:
            if self._hosts:
                dialogs.toast(self, "No hosts included", "warning",
                              detail="Switch a host on in the sidebar to include it.")
            else:
                self._log.log("No hosts configured.", "warn")
            return
        if confirm and not dialogs.askyesno(
            f"{action} on {len(hosts)} host{'' if len(hosts) == 1 else 's'}?", confirm,
            parent=self, confirm=confirm_label or "Continue",
        ):
            return

        self._set_bulk_enabled(False)
        button.configure(text=busy_text)
        self._bulk_running = True
        self._show_progress(True)
        if not self._busy:
            self._set_activity(f"{action}: {len(hosts)} host{'' if len(hosts) == 1 else 's'}…")
        skipped = len(self._hosts) - len(hosts)
        self._log.log(
            f"{action}: starting on {len(hosts)} host(s)…"
            + (f" ({skipped} switched off, skipped)" if skipped else "")
        )

        remaining = len(hosts)
        lock = threading.Lock()

        # Several hosts report at once and their lines interleave. Each gets an
        # identity colour — on its name in the log and on its dot in the sidebar
        # — and the names are padded to one width so the messages line up.
        colors = _host_colors(self._hosts)
        column = min(_SOURCE_COLUMN_MAX, max(len(host.display_name) for host in hosts))
        for host in hosts:
            self._host_list.mark(host, colors[id(host)], working=True)

        def run_one(host: Host) -> None:
            nonlocal remaining
            color = colors[id(host)]
            source = _source_label(host.display_name, column)

            def log(message: str, level: str = "info") -> None:
                self._log.log(message, level, source=source, color=color)

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
                self.after(0, lambda: self._host_list.mark(host, color, working=False))
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
        self._bulk_running = False
        self._show_progress(self._busy or self._connecting)
        if not self._busy:
            self._set_activity("")
        self._save_hosts_soon()
        self._host_list.clear_marks()
        if self._active is not None:
            self._update_connection_display()
        self._log.log(f"{action}: finished on all hosts.", "success")
        included = self._bulk_hosts()
        failed = sum(1 for h in included if h.pending_updates == UPDATES_FAILED)
        dialogs.toast(
            self, f"{action} finished", "warning" if failed else "success",
            detail=f"{failed} host(s) could not be reached or checked" if failed
            else f"{len(included)} host{'' if len(included) == 1 else 's'} done",
        )

    def _set_bulk_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled and self._bulk_hosts() else "disabled"
        self._btn_check_all.configure(state=state, text="Check for updates")
        self._btn_update_all.configure(state=state, text="Update all hosts")

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, activity: str = "") -> None:
        self._busy = busy
        self.configure(cursor="watch" if busy else "")
        self._show_progress(busy or self._connecting or self._bulk_running)
        self._set_activity(activity if busy else "")

    def _set_activity(self, text: str) -> None:
        """Show ``text`` with a spinner in the header, or hide both when empty."""
        if text:
            self._activity_text.configure(text=text)
            if not self._activity.winfo_ismapped():
                self._activity.pack(side="left", padx=(16, 0))
            self._spinner.start()
        else:
            self._spinner.stop()
            self._activity.pack_forget()

    def _show_progress(self, active: bool) -> None:
        """Run or park the hairline progress bar under the header."""
        if active == getattr(self, "_progress_on", False):
            return
        self._progress_on = active
        if active:
            self._progress.configure(fg_color=theme.SURFACE_2, progress_color=theme.ACCENT)
            self._progress.start()
        else:
            self._progress.stop()
            self._progress.configure(fg_color=theme.BG, progress_color=theme.BG)

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

    # ──────────────────────────────────────────────────────────────────────────
    # Remembered window state
    # ──────────────────────────────────────────────────────────────────────────

    def _restore_window_state(self) -> None:
        """Reopen where the window was left, on the host that was selected."""
        saved = settings.load()
        self._settings = saved
        geometry = settings.usable_geometry(saved.get("geometry", ""),
                                            settings.virtual_screen(self))
        if geometry:
            self.geometry(geometry)
        if saved.get("maximized") and sys.platform.startswith("win"):
            self.after(50, lambda: self.state("zoomed"))
        last = saved.get("last_host")
        host = next((h for h in self._hosts if _host_key(h) == last), None)
        if host is not None:
            # Select only. Connecting on launch would surprise anyone whose host
            # has gone away since, and an SSH attempt is not free.
            self.after(10, lambda: self._select_host(host))

    def _save_window_state(self) -> None:
        data = dict(getattr(self, "_settings", {}))
        try:
            maximized = self.state() == "zoomed"
            data["maximized"] = maximized
            if not maximized:
                # A maximised window's geometry is the screen; keep the size it
                # will return to instead.
                data["geometry"] = self.geometry()
        except tk.TclError:
            pass
        data["last_host"] = _host_key(self._active) if self._active else None
        settings.save(data)

    def _on_close(self) -> None:
        self._save_window_state()
        self._disconnect()
        try:
            # Let the background writer finish first, then write synchronously.
            # The other order let a deferred (older) payload land *after* the
            # final save and undo the last change made before closing.
            flush_pending_saves()
            save_hosts(self._hosts)
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


def _host_key(host: Host) -> str:
    """A stable identity for a host across runs: what you connect to, not its label."""
    return f"{host.username}@{host.hostname}:{host.port}"


def _bulk_update_prompt(included: list[Host], total: int) -> str:
    """Body of the "Update all hosts" confirmation, naming who is left out."""
    plural = "" if len(included) == 1 else "s"
    text = (f"OS packages will be upgraded on {len(included)} host{plural}, several at a "
            f"time. This can take a few minutes per host.")
    skipped = total - len(included)
    if skipped:
        text += f" {skipped} host{'' if skipped == 1 else 's'} switched off in the sidebar will be skipped."
    return text


_SOURCE_COLUMN_MAX = 16
"""Widest the log's host-name column grows; longer names are cut to fit it."""


def _host_colors(hosts: list[Host]) -> dict[int, str]:
    """An identity colour per host, keyed by ``id``.

    Assigned by position in the full list rather than among the hosts taking
    part, so a host keeps its colour from one bulk run to the next whichever of
    its neighbours are switched off.
    """
    return {id(host): theme.host_color(index) for index, host in enumerate(hosts)}


def _source_label(name: str, width: int) -> str:
    """``name`` cut or padded to exactly ``width`` characters, for a log column."""
    if len(name) > width:
        name = name[:max(width - 1, 0)] + "…"
    return name.ljust(width)


def _first_sentence(text: str) -> str:
    """The opening sentence of an error, short enough for a toast."""
    head = (text or "").strip().split("\n", 1)[0]
    for mark in (". ", " — "):
        if mark in head:
            head = head.split(mark, 1)[0]
    return head[:140]



def _load_logo(root: tk.Misc, size: int) -> Optional[tk.PhotoImage]:
    """The sidebar logo at the closest pre-rendered size for the display scaling.

    Tk scales nothing itself, and CTkImage would pull in Pillow just for this.
    """
    try:
        scaling = ctk.ScalingTracker.get_window_scaling(root)
    except (AttributeError, KeyError, tk.TclError):
        scaling = 1.0
    wanted = size * scaling
    rendered = min((30, 45, 60), key=lambda px: abs(px - wanted))
    path = theme.ASSETS / f"logo-{rendered}.png"
    try:
        return tk.PhotoImage(master=root, file=str(path))
    except (tk.TclError, OSError):
        return None
