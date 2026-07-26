"""Modal dialog for adding or editing a host."""

import os
from typing import Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

MIN_PORT = 1
MAX_PORT = 65535


def expand_key_path(value: str) -> str:
    """Resolve ``~`` and environment variables in a key path, and unquote it.

    The field's own placeholder suggests ``~/.ssh/id_rsa``, and an SSH config
    import supplies paths in the same form — but neither :func:`os.path.isfile`
    nor paramiko expands a tilde, so taking the text literally rejected a key
    that was sitting right there.
    """
    cleaned = value.strip().strip('"')
    if not cleaned:
        return ""
    return os.path.expandvars(os.path.expanduser(cleaned))


def parse_port(value: str) -> Optional[int]:
    """Parse a port, or ``None`` if it is not a usable one.

    Range-checked, not merely numeric: ``0`` and ``70000`` parse fine as integers
    and then fail at connect time with an error about the host rather than the
    port that was actually wrong.
    """
    try:
        port = int(value.strip())
    except (AttributeError, ValueError):
        return None
    return port if MIN_PORT <= port <= MAX_PORT else None


class HostDialog(ctk.CTkToplevel):
    """Collects host connection details.

    On save, :attr:`result` holds a dict of the entered values; it stays ``None``
    if the user cancelled. The dialog never touches the keyring or the host
    object itself — the caller decides what to do with the values.
    """

    def __init__(self, parent, host=None):
        super().__init__(parent)
        self.result: dict | None = None
        self._host = host

        self.title("Edit Host" if host else "Add Host")
        self.geometry("440x520")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.focus_force()

        self._build()
        if host:
            self._populate(host)

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self):
        pad = {"padx": 16, "pady": 4}

        ctk.CTkLabel(self, text="Label (optional)").pack(anchor="w", **pad)
        self._label = ctk.CTkEntry(self, placeholder_text="e.g. Media Server")
        self._label.pack(fill="x", **pad)

        ctk.CTkLabel(self, text="Hostname / IP *").pack(anchor="w", **pad)
        self._host_entry = ctk.CTkEntry(self, placeholder_text="192.168.1.10 or server.local")
        self._host_entry.pack(fill="x", **pad)

        ctk.CTkLabel(self, text="Port").pack(anchor="w", **pad)
        self._port = ctk.CTkEntry(self, placeholder_text="22")
        self._port.pack(fill="x", **pad)

        ctk.CTkLabel(self, text="Username *").pack(anchor="w", **pad)
        self._user = ctk.CTkEntry(self, placeholder_text="root")
        self._user.pack(fill="x", **pad)

        # ── SSH Key ───────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="SSH Key File (optional)").pack(anchor="w", **pad)
        key_row = ctk.CTkFrame(self, fg_color="transparent")
        key_row.pack(fill="x", padx=16, pady=4)
        key_row.grid_columnconfigure(0, weight=1)

        self._key_var = tk.StringVar()
        self._key_entry = ctk.CTkEntry(key_row, textvariable=self._key_var, placeholder_text="~/.ssh/id_rsa")
        self._key_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(key_row, text="Browse", width=70, command=self._browse_key).grid(row=0, column=1)

        # ── Password ─────────────────────────────────────────────────────────
        self._pass_label = ctk.CTkLabel(self, text=self._password_hint())
        self._pass_label.pack(anchor="w", **pad)
        self._pass = ctk.CTkEntry(self, show="*", placeholder_text="password")
        self._pass.pack(fill="x", **pad)

        # Whether a password is required depends on whether a key is set, so the
        # hint has to follow the key field as it is typed or browsed to.
        self._key_var.trace_add("write", self._on_key_changed)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=14)
        ctk.CTkButton(btn_frame, text="Cancel", fg_color="gray40", command=self._cancel).pack(side="right", padx=4)
        ctk.CTkButton(btn_frame, text="Save", command=self._save).pack(side="right")

    def _password_hint(self) -> str:
        """Label for the password field, which means different things per mode."""
        if self._host is not None:
            return "Password (leave blank to keep the stored one)"
        if self._key_var.get().strip():
            return "Passphrase (optional — only if the key needs one)"
        return "Password (required without a key)"

    def _on_key_changed(self, *_):
        self._pass_label.configure(text=self._password_hint())

    def _browse_key(self):
        start = os.path.expanduser("~/.ssh")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            parent=self,
            title="Select SSH private key",
            initialdir=start,
            filetypes=[("Private key files", "id_rsa id_ed25519 id_ecdsa *.pem *.key *"), ("All files", "*.*")],
        )
        if path:
            self._key_var.set(path)

    def _populate(self, host):
        self._label.insert(0, host.label)
        self._host_entry.insert(0, host.hostname)
        self._port.insert(0, str(host.port))
        self._user.insert(0, host.username)
        if host.key_path:
            self._key_var.set(host.key_path)

    def _save(self):
        hostname = self._host_entry.get().strip()
        username = self._user.get().strip()
        password = self._pass.get()
        key_path = expand_key_path(self._key_var.get())
        port_str = self._port.get().strip() or "22"

        if not hostname:
            messagebox.showerror("Validation", "Hostname is required.", parent=self)
            return
        if not username:
            messagebox.showerror("Validation", "Username is required.", parent=self)
            return

        # Need either a key or a password (new hosts must supply one; edits may keep existing)
        adding_new = self._host is None
        if adding_new and not key_path and not password:
            messagebox.showerror("Validation", "Provide a password or an SSH key file.", parent=self)
            return

        if key_path and not os.path.isfile(key_path):
            messagebox.showerror("Validation", f"Key file not found:\n{key_path}", parent=self)
            return

        port = parse_port(port_str)
        if port is None:
            messagebox.showerror(
                "Validation",
                f"Port must be a whole number between {MIN_PORT} and {MAX_PORT}.",
                parent=self,
            )
            return

        self.result = {
            "hostname": hostname,
            "username": username,
            "password": password,
            "key_path": key_path,
            "port": port,
            "label": self._label.get().strip(),
        }
        self.destroy()

    def _cancel(self):
        self.destroy()
