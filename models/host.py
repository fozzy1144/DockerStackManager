"""Data models for managed hosts and the Docker Compose stacks found on them."""

from dataclasses import dataclass, field, fields
from typing import Optional

# Sentinel values for Host.pending_updates. Real counts are >= 0.
UPDATES_UNKNOWN = -1
"""No update check has run against this host yet."""

UPDATES_FAILED = -2
"""The last update check could not complete (unreachable, or unparsable output)."""


@dataclass(frozen=True, slots=True)
class ImageStatus:
    """Whether one of a stack's images has a newer version in its registry."""

    image: str
    local_digest: str = ""
    remote_digest: str = ""
    update_available: Optional[bool] = None
    """``None`` when it could not be determined — never guessed."""

    detail: str = ""
    """Why it is unknown, or how the comparison was made."""

    @property
    def label(self) -> str:
        if self.update_available is None:
            return "unknown"
        return "update available" if self.update_available else "up to date"


@dataclass(slots=True)
class DockerStack:
    """One Compose project: a directory containing a compose file."""

    name: str
    """Display name — the Compose project name when known, else the folder name."""

    path: str
    """Absolute directory holding the compose file. Commands run from here."""

    compose_file: str
    """Absolute path of the compose file itself."""

    status: str = "unknown"
    """One of ``running``, ``partial``, ``stopped``, ``unknown``."""

    project: str = ""
    """Compose project name as Docker reports it, when discovered from Docker."""

    image_snapshot: dict[str, str] = field(default_factory=dict)
    """Image reference to image ID, recorded before the last pull.

    This is what makes a rollback possible: after a pull replaces ``app:latest``
    the previous image is still on disk but untagged, and only its ID identifies
    it. Persisted, because a rollback is usually wanted in a later session than
    the update that caused it.
    """

    snapshot_taken: str = ""
    """When :attr:`image_snapshot` was recorded, for display. Empty if never."""

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "DockerStack":
        stack = _build(cls, data)
        # Tolerate a corrupted or foreign snapshot rather than failing the load.
        if not isinstance(stack.image_snapshot, dict):
            stack.image_snapshot = {}
        else:
            stack.image_snapshot = {
                str(k): str(v) for k, v in stack.image_snapshot.items()
            }
        return stack

    @property
    def can_roll_back(self) -> bool:
        return bool(self.image_snapshot)


def merge_stacks(
    previous: list[DockerStack], discovered: list[DockerStack]
) -> list[DockerStack]:
    """Return ``discovered``, carrying each stack's rollback point forward.

    A rescan builds fresh stacks from what the host reports, and the host cannot
    tell us which image versions preceded the last pull — that is recorded here,
    locally. Replacing the list wholesale therefore threw every rollback point
    away, including the one an update had just taken seconds earlier, since a
    rescan is exactly what follows an update.

    Matched on :attr:`DockerStack.path`, which is what identifies a project on
    the host. A stack whose directory has genuinely moved starts over, because
    the images its old location recorded may no longer be the ones it runs.
    """
    remembered = {
        stack.path.rstrip("/"): stack for stack in previous if stack.image_snapshot
    }
    for stack in discovered:
        earlier = remembered.get(stack.path.rstrip("/"))
        if earlier is not None and not stack.image_snapshot:
            stack.image_snapshot = dict(earlier.image_snapshot)
            stack.snapshot_taken = earlier.snapshot_taken
    return discovered


@dataclass(frozen=True, slots=True)
class Container:
    """One container belonging to a stack, as ``docker compose ps`` reports it.

    Runtime state only — never persisted.
    """

    name: str
    service: str
    state: str = ""
    """``running``, ``exited``, ``created``, ``paused``, ``restarting``…"""

    health: str = ""
    """``healthy``, ``unhealthy``, ``starting``, or empty when no healthcheck."""

    image: str = ""
    ports: str = ""
    """Published ports, pre-formatted for display."""

    exit_code: int = 0

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def status_label(self) -> str:
        """State with health folded in, e.g. ``running (unhealthy)``."""
        if self.health and self.state == "running":
            return f"{self.state} ({self.health})"
        if self.state == "exited" and self.exit_code:
            return f"exited ({self.exit_code})"
        return self.state or "unknown"


@dataclass(slots=True)
class Host:
    """A remote Linux host, plus whatever we have learned about it so far.

    Everything except :attr:`pending_updates` is persisted to ``hosts.json``;
    passwords live in the OS keyring and are never part of this object.
    """

    hostname: str
    username: str
    port: int = 22
    label: str = ""
    key_path: str = ""

    os_info: str = ""
    """``/etc/os-release`` ``ID``, e.g. ``"debian"``. Empty until detected."""

    os_pretty: str = ""
    """``PRETTY_NAME`` for display, e.g. ``"Debian GNU/Linux 12 (bookworm)"``."""

    os_like: str = ""
    """``ID_LIKE`` parents, used to pick a package manager for derivatives."""

    stacks: list[DockerStack] = field(default_factory=list)

    pending_updates: int = UPDATES_UNKNOWN
    """Runtime only, not persisted. Count, or one of the ``UPDATES_*`` sentinels."""

    @property
    def display_name(self) -> str:
        return self.label or self.hostname

    @property
    def address(self) -> str:
        """``user@host`` with the port appended when it is not the default."""
        base = f"{self.username}@{self.hostname}"
        return base if self.port == 22 else f"{base}:{self.port}"

    def to_dict(self) -> dict:
        data = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in ("stacks", "pending_updates")
        }
        data["stacks"] = [s.to_dict() for s in self.stacks]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Host":
        """Build a Host from persisted JSON, tolerating unknown or missing keys.

        Raises :class:`TypeError` only when ``hostname`` or ``username`` — the two
        fields without a sensible default — are absent; callers skip such
        entries rather than let one bad record stop the app from starting.
        """
        host = _build(cls, data)
        host.stacks = _load_stacks(data.get("stacks"))
        return host


def _load_stacks(raw: object) -> list[DockerStack]:
    """Build a host's stacks, skipping any record that will not load.

    Per-record tolerance, for the same reason :func:`gui.app._read_hosts` has it:
    one unusable stack should cost that stack, not the host it belongs to — which
    is what a record missing ``compose_file`` used to do, by raising out of
    :meth:`Host.from_dict` entirely.
    """
    if not isinstance(raw, list):
        return []
    stacks: list[DockerStack] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        try:
            stacks.append(DockerStack.from_dict(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return stacks


def _build(cls, data: dict):
    """Instantiate a dataclass from ``data``, ignoring keys it does not declare.

    Forward compatibility both ways: a config written by a newer version can
    still be read here, and fields added later simply take their defaults.
    """
    accepted = {f.name for f in fields(cls)} - {"stacks"}
    return cls(**{k: v for k, v in data.items() if k in accepted})
