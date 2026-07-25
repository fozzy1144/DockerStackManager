"""Data models for managed hosts and the Docker Compose stacks found on them."""

from dataclasses import dataclass, field, fields

# Sentinel values for Host.pending_updates. Real counts are >= 0.
UPDATES_UNKNOWN = -1
"""No update check has run against this host yet."""

UPDATES_FAILED = -2
"""The last update check could not complete (unreachable, or unparsable output)."""


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

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "DockerStack":
        return _build(cls, data)


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

        Raises :class:`KeyError` only when ``hostname`` or ``username`` — the two
        fields without a sensible default — are absent; callers skip such
        entries rather than let one bad record stop the app from starting.
        """
        host = _build(cls, data)
        host.stacks = [
            DockerStack.from_dict(s)
            for s in data.get("stacks") or []
            if isinstance(s, dict) and s.get("path")
        ]
        return host


def _build(cls, data: dict):
    """Instantiate a dataclass from ``data``, ignoring keys it does not declare.

    Forward compatibility both ways: a config written by a newer version can
    still be read here, and fields added later simply take their defaults.
    """
    accepted = {f.name for f in fields(cls)} - {"stacks"}
    return cls(**{k: v for k, v in data.items() if k in accepted})
