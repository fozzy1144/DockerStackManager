import threading
import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from models.host import Host, DockerStack
from core.credentials import (
    save_password, get_password, delete_password,
    save_hosts, load_hosts,
)
from core.ssh_client import SSHClient
from gui.host_dialog import HostDialog
from gui.log_panel import LogPanel

OS_COLORS = {
    "ubuntu": "#E95420",
    "debian": "#A80030",
    "fedora": "#3C6EB4",
    "centos": "#932279",
    "rhel": "#EE0000",
    "rocky": "#10B981",
    "almalinux": "#083F8A",
    "arch": "#1793D1",
    "manjaro": "#35BF5C",
    "alpine": "#0D597F",
    "opensuse": "#73BA25",
}
DEFAULT_OS_COLOR = "#555577"

STATUS_ICONS = {"running": "●", "stopped": "○", "partial": "◑", "unknown": "?"}
STATUS_COLORS = {"running": "#4CAF50", "stopped": "#888888", "partial": "#FFA500", "unknown": "#AAAAAA"}


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Docker & System Update Manager")
        self.geometry("1200x760")
        self.minsize(900, 600)

        self._hosts: list[Host] = []
        self._active_host: Host | None = None
        self._ssh: SSHClient | None = None
        self._stack_vars: dict[str, tk.BooleanVar] = {}
        self._busy = False

        raw = load_hosts()
        self._hosts = [Host.from_dict(d) for d in raw]

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ──────────────────────────────────────────────────────────────────────────
    # UI Construction
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Left sidebar
        sidebar = ctk.CTkFrame(self, width=240, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            sidebar, text="Hosts", font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 4), sticky="w")

        self._host_list_frame = ctk.CTkScrollableFrame(sidebar, label_text="")
        self._host_list_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._host_list_frame.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkButton(btn_row, text="+ Add Host", command=self._add_host).pack(fill="x")

        # Right content
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)

        # Host info bar
        self._info_bar = ctk.CTkFrame(content, height=60)
        self._info_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._info_bar.grid_propagate(False)
        self._info_bar.grid_columnconfigure(1, weight=1)

        self._host_title = ctk.CTkLabel(
            self._info_bar, text="Select a host", font=ctk.CTkFont(size=18, weight="bold"),
        )
        self._host_title.grid(row=0, column=0, padx=14, pady=4, sticky="w")

        self._os_badge = ctk.CTkLabel(
            self._info_bar, text="", fg_color=DEFAULT_OS_COLOR, corner_radius=8, padx=8,
        )
        self._os_badge.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        action_row = ctk.CTkFrame(self._info_bar, fg_color="transparent")
        action_row.grid(row=0, column=2, padx=8, pady=4, sticky="e")

        self._btn_connect = ctk.CTkButton(
            action_row, text="Connect", width=100, command=self._toggle_connect
        )
        self._btn_connect.pack(side="left", padx=4)

        self._btn_scan = ctk.CTkButton(
            action_row, text="Scan Stacks", width=110,
            command=self._scan_stacks, state="disabled"
        )
        self._btn_scan.pack(side="left", padx=4)

        self._btn_sys_update = ctk.CTkButton(
            action_row, text="System Update", width=120,
            fg_color="#7B5EA7", hover_color="#6A4D96",
            command=self._run_system_update, state="disabled"
        )
        self._btn_sys_update.pack(side="left", padx=4)

        # Main split: stacks + log
        main_split = ctk.CTkFrame(content, fg_color="transparent")
        main_split.grid(row=1, column=0, sticky="nsew")
        main_split.grid_rowconfigure(0, weight=1)
        main_split.grid_columnconfigure(0, weight=2)
        main_split.grid_columnconfigure(1, weight=3)

        # Stack panel
        stack_panel = ctk.CTkFrame(main_split)
        stack_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        stack_panel.grid_rowconfigure(1, weight=1)
        stack_panel.grid_columnconfigure(0, weight=1)

        sp_header = ctk.CTkFrame(stack_panel, fg_color="transparent")
        sp_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(sp_header, text="Docker Stacks", font=ctk.CTkFont(weight="bold")).pack(side="left")

        sp_btns = ctk.CTkFrame(sp_header, fg_color="transparent")
        sp_btns.pack(side="right")
        ctk.CTkButton(
            sp_btns, text="All", width=44, height=24,
            command=lambda: self._select_all_stacks(True)
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            sp_btns, text="None", width=44, height=24,
            command=lambda: self._select_all_stacks(False)
        ).pack(side="left", padx=2)

        self._stack_scroll = ctk.CTkScrollableFrame(stack_panel, label_text="")
        self._stack_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._stack_scroll.grid_columnconfigure(0, weight=1)

        self._btn_update_stacks = ctk.CTkButton(
            stack_panel, text="Update Selected Stacks",
            state="disabled", command=self._update_selected_stacks,
        )
        self._btn_update_stacks.grid(row=2, column=0, padx=8, pady=8, sticky="ew")

        self._log = LogPanel(main_split)
        self._log.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self._refresh_host_list()

    # ──────────────────────────────────────────────────────────────────────────
    # Host management
    # ──────────────────────────────────────────────────────────────────────────

    def _add_host(self):
        dlg = HostDialog(self)
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        host = Host(
            hostname=r["hostname"],
            username=r["username"],
            port=r["port"],
            label=r["label"],
            key_path=r["key_path"],
        )
        if r["password"]:
            save_password(host.hostname, host.username, r["password"])
        self._hosts.append(host)
        save_hosts(self._hosts)
        self._refresh_host_list()
        self._select_host(host)

    def _edit_host(self, host: Host):
        dlg = HostDialog(self, host=host)
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        old_key = (host.hostname, host.username)
        host.hostname = r["hostname"]
        host.username = r["username"]
        host.port = r["port"]
        host.label = r["label"]
        host.key_path = r["key_path"]
        if r["password"]:
            delete_password(*old_key)
            save_password(host.hostname, host.username, r["password"])
        save_hosts(self._hosts)
        self._refresh_host_list()
        if self._active_host is host:
            self._host_title.configure(text=host.display_name)

    def _remove_host(self, host: Host):
        if not messagebox.askyesno("Remove Host", f"Remove '{host.display_name}'?", parent=self):
            return
        if self._active_host is host:
            self._disconnect_ssh()
            self._active_host = None
            self._clear_stack_panel()
            self._host_title.configure(text="Select a host")
            self._os_badge.configure(text="")
        delete_password(host.hostname, host.username)
        self._hosts.remove(host)
        save_hosts(self._hosts)
        self._refresh_host_list()

    def _refresh_host_list(self):
        for w in self._host_list_frame.winfo_children():
            w.destroy()

        for host in self._hosts:
            card = ctk.CTkFrame(self._host_list_frame, corner_radius=8)
            card.pack(fill="x", pady=3)
            card.grid_columnconfigure(0, weight=1)
            card.configure(fg_color="#1E6B9E" if host is self._active_host else "transparent")

            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
            inner.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(inner, text=host.display_name, font=ctk.CTkFont(weight="bold"), anchor="w").grid(
                row=0, column=0, sticky="w"
            )

            sub = f"{host.username}@{host.hostname}"
            if host.port != 22:
                sub += f":{host.port}"
            ctk.CTkLabel(inner, text=sub, font=ctk.CTkFont(size=11), text_color="gray70", anchor="w").grid(
                row=1, column=0, sticky="w"
            )

            if host.os_pretty:
                ctk.CTkLabel(inner, text=host.os_pretty, font=ctk.CTkFont(size=11), text_color="gray60", anchor="w").grid(
                    row=2, column=0, sticky="w"
                )

            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.grid(row=0, column=1, padx=4, pady=4)
            ctk.CTkButton(btn_frame, text="✎", width=28, height=28, command=lambda h=host: self._edit_host(h)).pack(pady=1)
            ctk.CTkButton(
                btn_frame, text="✕", width=28, height=28,
                fg_color="#883333", hover_color="#aa4444",
                command=lambda h=host: self._remove_host(h),
            ).pack(pady=1)

            for widget in (card, inner) + tuple(inner.winfo_children()):
                widget.bind("<Button-1>", lambda e, h=host: self._select_host(h))

    # ──────────────────────────────────────────────────────────────────────────
    # Host selection & connection
    # ──────────────────────────────────────────────────────────────────────────

    def _select_host(self, host: Host):
        if self._busy or self._active_host is host:
            return
        self._disconnect_ssh()
        self._active_host = host
        self._refresh_host_list()
        self._host_title.configure(text=host.display_name)
        self._os_badge.configure(
            text=host.os_pretty or "Unknown OS",
            fg_color=OS_COLORS.get(host.os_info, DEFAULT_OS_COLOR),
        )
        self._btn_connect.configure(text="Connect", state="normal")
        self._btn_scan.configure(state="disabled")
        self._btn_sys_update.configure(state="disabled")
        self._btn_update_stacks.configure(state="disabled")
        self._clear_stack_panel()
        self._populate_stacks(host.stacks)

    def _toggle_connect(self):
        if not self._active_host:
            return
        if self._ssh and self._ssh.is_connected:
            self._disconnect_ssh()
        else:
            self._connect_ssh()

    def _connect_ssh(self):
        host = self._active_host
        password = get_password(host.hostname, host.username) or ""
        if not host.key_path and not password:
            messagebox.showerror("No credentials", f"No saved password or SSH key for {host.display_name}.", parent=self)
            return

        self._set_busy(True)
        self._log.log(f"Connecting to {host.display_name} ({host.hostname}:{host.port})…")
        if host.key_path:
            self._log.log(f"Using key: {host.key_path}")
        self._btn_connect.configure(text="Connecting…", state="disabled")

        ssh = SSHClient(host.hostname, host.username, password, host.port, key_path=host.key_path)

        def task():
            ok, msg = ssh.connect()
            self.after(0, lambda: self._on_connected(ssh, ok, msg))

        threading.Thread(target=task, daemon=True).start()

    def _on_connected(self, ssh: SSHClient, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self._log.log(f"Connection failed: {msg}", "error")
            self._btn_connect.configure(text="Connect", state="normal")
            return
        self._ssh = ssh
        self._log.log("Connected.", "success")
        self._btn_connect.configure(text="Disconnect", state="normal")
        self._btn_scan.configure(state="normal")
        self._btn_sys_update.configure(state="normal")
        if not self._active_host.os_pretty:
            self._detect_os()

    def _disconnect_ssh(self):
        if self._ssh:
            self._ssh.disconnect()
            self._ssh = None
        self._btn_connect.configure(text="Connect", state="normal")
        self._btn_scan.configure(state="disabled")
        self._btn_sys_update.configure(state="disabled")

    def _detect_os(self):
        host = self._active_host
        ssh = self._ssh  # capture now — avoids use-after-disconnect in thread
        if not ssh:
            return

        def task():
            os_id, pretty = ssh.detect_os()
            self.after(0, lambda: self._on_os_detected(host, os_id, pretty))

        threading.Thread(target=task, daemon=True).start()

    def _on_os_detected(self, host: Host, os_id: str, pretty: str):
        host.os_info = os_id
        host.os_pretty = pretty
        save_hosts(self._hosts)
        self._os_badge.configure(text=pretty, fg_color=OS_COLORS.get(os_id, DEFAULT_OS_COLOR))
        self._log.log(f"OS detected: {pretty}", "info")
        self._refresh_host_list()

    # ──────────────────────────────────────────────────────────────────────────
    # Stack management
    # ──────────────────────────────────────────────────────────────────────────

    def _scan_stacks(self):
        if not self._ssh or not self._active_host:
            return
        self._set_busy(True)
        self._btn_scan.configure(state="disabled", text="Scanning…")
        self._log.log("Scanning for Docker stacks…")

        host = self._active_host
        ssh = self._ssh

        def task():
            stacks = ssh.find_docker_stacks(
                log_cb=lambda m: self.after(0, lambda msg=m: self._log.log(msg))
            )
            self.after(0, lambda: self._on_stacks_found(host, stacks))

        threading.Thread(target=task, daemon=True).start()

    def _on_stacks_found(self, host: Host, stacks: list[DockerStack]):
        self._set_busy(False)
        self._btn_scan.configure(state="normal", text="Scan Stacks")
        # Guard: user may have switched hosts while scan was running
        if host is not self._active_host:
            return
        host.stacks = stacks
        save_hosts(self._hosts)
        self._clear_stack_panel()
        self._populate_stacks(stacks)
        self._log.log(f"Found {len(stacks)} stack(s).", "success")
        if stacks:
            self._btn_update_stacks.configure(state="normal")

    def _clear_stack_panel(self):
        for w in self._stack_scroll.winfo_children():
            w.destroy()
        self._stack_vars.clear()

    def _populate_stacks(self, stacks: list[DockerStack]):
        self._clear_stack_panel()
        if not stacks:
            ctk.CTkLabel(
                self._stack_scroll,
                text="No stacks found.\nConnect and scan to discover.",
                text_color="gray60",
            ).pack(pady=20)
            return
        for stack in stacks:
            self._add_stack_row(stack)
        if self._ssh and self._ssh.is_connected:
            self._btn_update_stacks.configure(state="normal")

    def _add_stack_row(self, stack: DockerStack):
        var = tk.BooleanVar(value=True)
        self._stack_vars[stack.path] = var

        row = ctk.CTkFrame(self._stack_scroll, corner_radius=6)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(row, text="", variable=var, width=24).grid(row=0, column=0, padx=(8, 2), pady=6)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(info, text=stack.name, font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(info, text=stack.path, font=ctk.CTkFont(size=11), text_color="gray60", anchor="w").pack(fill="x")

        icon = STATUS_ICONS.get(stack.status, "?")
        color = STATUS_COLORS.get(stack.status, "#AAAAAA")
        ctk.CTkLabel(row, text=f"{icon} {stack.status}", text_color=color, font=ctk.CTkFont(size=12)).grid(
            row=0, column=2, padx=10
        )

    def _select_all_stacks(self, value: bool):
        for var in self._stack_vars.values():
            var.set(value)

    # ──────────────────────────────────────────────────────────────────────────
    # Updates
    # ──────────────────────────────────────────────────────────────────────────

    def _update_selected_stacks(self):
        if not self._ssh or not self._active_host:
            return
        selected_paths = {path for path, var in self._stack_vars.items() if var.get()}
        if not selected_paths:
            messagebox.showinfo("Nothing selected", "Select at least one stack to update.", parent=self)
            return

        stacks_to_update = [s for s in self._active_host.stacks if s.path in selected_paths]
        count = len(stacks_to_update)
        if not messagebox.askyesno(
            "Confirm Update",
            f"Update {count} stack(s) on '{self._active_host.display_name}'?\nThis will pull new images and recreate containers.",
            parent=self,
        ):
            return

        self._set_busy(True)
        self._btn_update_stacks.configure(state="disabled", text="Updating…")
        self._log.log(f"Starting update of {count} stack(s)…")
        ssh = self._ssh

        def task():
            results = []
            for stack in stacks_to_update:
                ok = ssh.update_stack(
                    stack,
                    log_cb=lambda m: self.after(0, lambda msg=m: self._log.log(msg)),
                )
                results.append((stack.name, ok))
            self.after(0, lambda: self._on_update_done(results))

        threading.Thread(target=task, daemon=True).start()

    def _on_update_done(self, results: list[tuple[str, bool]]):
        self._set_busy(False)
        self._btn_update_stacks.configure(state="normal", text="Update Selected Stacks")
        passed = sum(1 for _, ok in results if ok)
        failed = len(results) - passed
        level = "success" if failed == 0 else "warn" if passed > 0 else "error"
        self._log.log(f"Update complete: {passed} succeeded, {failed} failed.", level)
        self._scan_stacks()

    def _run_system_update(self):
        if not self._ssh or not self._active_host:
            return
        host = self._active_host
        if not messagebox.askyesno(
            "System Update",
            f"Run a full system package update on '{host.display_name}'?\nThis may take several minutes.",
            parent=self,
        ):
            return

        self._set_busy(True)
        self._log.log("Starting system update…")
        ssh = self._ssh

        def task():
            ok = ssh.run_system_update(
                host.os_info or "unknown",
                log_cb=lambda m: self.after(0, lambda msg=m: self._log.log(msg)),
            )
            level = "success" if ok else "error"
            self.after(0, lambda: self._log.log("System update finished.", level))
            self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=task, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.configure(cursor="watch" if busy else "")

    def _on_close(self):
        self._disconnect_ssh()
        save_hosts(self._hosts)
        self.destroy()
