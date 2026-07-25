"""On-disk and OS-keyring storage for host configuration and passwords.

Two kinds of state, deliberately kept apart:

* **Secrets** (SSH passwords and key passphrases) go to the OS credential store
  via :mod:`keyring` — Windows Credential Manager, macOS Keychain, or Secret
  Service on Linux. They are never written to a file by this application.
* **Configuration** (hostnames, usernames, ports, key paths, discovered stacks)
  goes to ``~/.docker_stack_manager/hosts.json``.

Both are best-effort: a missing keyring backend or a corrupt config file
degrades the feature that needs it rather than taking the application down.
"""

import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional

import keyring
import keyring.errors

ErrorHandler = Callable[[Exception], None]

_SERVICE_NAME = "DockerStackManager"

CONFIG_DIR: Path = Path.home() / ".docker_stack_manager"
"""Directory holding all persistent application state."""

HOSTS_FILE: Path = CONFIG_DIR / "hosts.json"
"""Host configuration. Contains no secrets."""

KNOWN_HOSTS_FILE: Path = CONFIG_DIR / "known_hosts"
"""SSH host keys accepted through this app, so key *changes* get noticed."""


def config_dir() -> Path:
    """Create the config directory if needed and return it."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def _account(hostname: str, username: str) -> str:
    return f"{username}@{hostname}"


# ──────────────────────────────────────────────────────────────────────────────
# Secrets
# ──────────────────────────────────────────────────────────────────────────────


def save_password(hostname: str, username: str, password: str) -> bool:
    """Store a password/passphrase in the OS keyring. False if unavailable."""
    try:
        keyring.set_password(_SERVICE_NAME, _account(hostname, username), password)
        return True
    except keyring.errors.KeyringError:
        return False


def get_password(hostname: str, username: str) -> str:
    """Return the stored password, or an empty string if there is none."""
    try:
        return keyring.get_password(_SERVICE_NAME, _account(hostname, username)) or ""
    except keyring.errors.KeyringError:
        return ""


def delete_password(hostname: str, username: str) -> None:
    """Remove a stored password. Silent if there was nothing to remove."""
    try:
        keyring.delete_password(_SERVICE_NAME, _account(hostname, username))
    except keyring.errors.KeyringError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────


def save_hosts(hosts: list) -> None:
    """Persist hosts to :data:`HOSTS_FILE`, synchronously.

    Writes to a sibling temp file and renames it over the target, so an
    interrupted write (or a crash mid-save) leaves the previous config intact
    rather than a truncated file the next launch cannot parse.
    """
    _write_payload(serialize_hosts(hosts))


def serialize_hosts(hosts: list) -> str:
    """Render hosts to JSON. Cheap, and safe to call on the UI thread."""
    return json.dumps([h.to_dict() for h in hosts], indent=2)


def save_hosts_async(hosts: list, on_error: Optional[ErrorHandler] = None) -> None:
    """Serialise now, write on a background thread.

    Serialising on the calling thread is what makes this safe: the snapshot is
    taken while the caller still owns the host objects, so a later mutation
    cannot tear the file. Only the disk write — the slow part — is deferred.
    Bursts coalesce, so a rapid sequence of edits costs one write.
    """
    _SAVER.submit(serialize_hosts(hosts), on_error)


def flush_pending_saves(timeout: float = 5.0) -> None:
    """Wait for any deferred write to finish. Call before exiting."""
    _SAVER.flush(timeout)


def _write_payload(payload: str) -> None:
    config_dir()
    tmp = HOSTS_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        _restrict_permissions(tmp)
        os.replace(tmp, HOSTS_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


class _AsyncSaver:
    """Writes the most recent payload on a single background thread.

    Only the latest snapshot matters, so a burst of edits collapses to one write
    rather than queueing several. The worker exits once it finds nothing pending,
    and is restarted on the next submission.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Optional[str] = None
        self._on_error: Optional[ErrorHandler] = None
        self._worker: Optional[threading.Thread] = None

    def submit(self, payload: str, on_error: Optional[ErrorHandler]) -> None:
        with self._lock:
            self._pending = payload
            self._on_error = on_error
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, name="config-writer", daemon=True
                )
                self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                payload, on_error = self._pending, self._on_error
                self._pending = None
                if payload is None:
                    # Clearing this under the same lock submit() takes is what
                    # stops a submission slipping in as the worker exits.
                    self._worker = None
                    return
            try:
                _write_payload(payload)
            except OSError as exc:
                if on_error is not None:
                    on_error(exc)

    def flush(self, timeout: float) -> None:
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)


_SAVER = _AsyncSaver()


def load_hosts() -> list[dict]:
    """Return the raw host dicts from disk, or ``[]`` if absent or unreadable."""
    try:
        data = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions. A no-op where chmod has no meaning."""
    try:
        path.chmod(0o600)
    except OSError:
        pass
