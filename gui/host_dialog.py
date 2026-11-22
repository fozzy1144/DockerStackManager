"""Modal dialog for adding or editing a host."""

import os
from typing import Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog

from gui import theme

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

        self.title("Edit host" if host else "Add host")
        self.resizable(False, False)
        self.transient(parent)
        theme.present(self, parent, (480, 600),
                      focus=lambda: (self._label if host else self._host_entry).focus_set())
        self.grab_set()

        self._build()
        if host:
            self._populate(host)

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=24, pady=(22, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)

        theme.heading(body, "Edit host" if self._host else "Add a host", role="title").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        theme.caption(
            body,
            "Connection details for a Linux machine you reach over SSH.",
            role="small",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 16))

        self._label = self._field(body, 2, "Display name", "e.g. Media server", hint="optional")

        # Hostname and port share a row: they are one address.
        theme.text(body, "Hostname or IP", "small_strong", theme.TEXT_2).grid(
            row=4, column=0, sticky="w", pady=(12, 4)
        )
        theme.text(body, "Port", "small_strong", theme.TEXT_2).grid(
            row=4, column=1, sticky="w", pady=(12, 4), padx=(10, 0)
        )
        self._host_entry = self._entry(body, "192.168.1.10 or server.local")
        self._host_entry.grid(row=5, column=0, sticky="ew")
        self._port = self._entry(body, "22", width=90)
        self._port.grid(row=5, column=1, sticky="ew", padx=(10, 0))

        self._user = self._field(body, 6, "Username", "root")

        theme.divider(body).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(18, 4))
        theme.overline(body, "Authentication").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        # ── SSH Key ───────────────────────────────────────────────────────────
        theme.text(body, "Private key file", "small_strong", theme.TEXT_2).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(10, 4)
        )
        key_row = ctk.CTkFrame(body, fg_color="transparent")
        key_row.grid(row=11, column=0, columnspan=2, sticky="ew")
        key_row.grid_columnconfigure(0, weight=1)
        self._key_var = tk.StringVar()
        self._key_entry = self._entry(key_row, "~/.ssh/id_ed25519", textvariable=self._key_var)
        self._key_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        theme.button(key_row, "Browse…", self._browse_key, size="md").grid(row=0, column=1)

        # ── Password ─────────────────────────────────────────────────────────
        self._pass_label = theme.text(body, self._password_hint(), "small_strong", theme.TEXT_2)
        self._pass_label.grid(row=12, column=0, columnspan=2, sticky="w", pady=(12, 4))
        pass_row = ctk.CTkFrame(body, fg_color="transparent")
        pass_row.grid(row=13, column=0, columnspan=2, sticky="ew")
        pass_row.grid_columnconfigure(0, weight=1)
        self._pass = self._entry(pass_row, "password", show="•")
        self._pass.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._reveal = theme.button(pass_row, "Show", self._toggle_reveal, variant="ghost",
                                    width=64)
        self._reveal.grid(row=0, column=1)
        theme.caption(
            body, "Stored in the system credential manager, never on disk.", role="caption"
        ).grid(row=14, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Whether a password is required depends on whether a key is set, so the
        # hint has to follow the key field as it is typed or browsed to.
        self._key_var.trace_add("write", self._on_key_changed)

        # ── Error + buttons ───────────────────────────────────────────────────
        self._error = theme.caption(self, "", color=theme.DANGER, role="small", wraplength=430)
        self._error.grid(row=1, column=0, sticky="ew", padx=24, pady=(14, 0))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, sticky="ew", padx=24, pady=(10, 22))
        theme.button(btn_frame, "Save host" if not self._host else "Save changes",
                     self._save, variant="primary").pack(side="right")
        theme.button(btn_frame, "Cancel", self._cancel, variant="ghost").pack(
            side="right", padx=(0, 8)
        )

    def _field(self, parent, row: int, label: str, placeholder: str, hint: str = ""):
        heading = ctk.CTkFrame(parent, fg_color="transparent")
        heading.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        theme.text(heading, label, "small_strong", theme.TEXT_2).pack(side="left")
        if hint:
            theme.caption(heading, hint).pack(side="left", padx=(6, 0))
        entry = self._entry(parent, placeholder)
        entry.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        return entry

    @staticmethod
    def _entry(parent, placeholder: str, **kwargs) -> ctk.CTkEntry:
        options = dict(placeholder_text=placeholder, height=36, font=theme.font("body"))
        options.update(kwargs)
        return ctk.CTkEntry(parent, **options)

    def _toggle_reveal(self):
        hidden = self._pass.cget("show") != ""
        self._pass.configure(show="" if hidden else "•")
        self._reveal.configure(text="Hide" if hidden else "Show")

    def _fail(self, message: str, field=None) -> None:
        """Show a validation problem in place, and put the cursor where it is."""
        self._error.configure(text=message)
        for entry in (self._host_entry, self._user, self._port, self._key_entry, self._pass):
            entry.configure(border_color=theme.BORDER_STRONG)
        if field is not None:
            field.configure(border_color=theme.DANGER)
            field.focus_set()

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
            self._fail("Enter the host's name or IP address.", self._host_entry)
            return

        port = parse_port(port_str)
        if port is None:
            self._fail(f"Port must be a whole number between {MIN_PORT} and {MAX_PORT}.",
                       self._port)
            return

        if not username:
            self._fail("Enter the user to log in as.", self._user)
            return

        # Need either a key or a password (new hosts must supply one; edits may keep existing)
        adding_new = self._host is None
        if adding_new and not key_path and not password:
            self._fail("Add a password or choose a private key file.", self._pass)
            return

        if key_path and not os.path.isfile(key_path):
            self._fail(f"No key file at {key_path}", self._key_entry)
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
