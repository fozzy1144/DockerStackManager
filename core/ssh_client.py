"""SSH transport: connect to a host, inspect it, and run commands on it.

This module is deliberately distro-agnostic — it knows how to *run* a command
and hand its output back, while :mod:`core.distro` decides *which* commands to
run. There are three execution paths:

* :meth:`SSHClient.run` — one-shot command, fully buffered. For fast probes.
* :meth:`SSHClient.stream` — long-running command whose output is delivered a
  line at a time, so the GUI can show progress while ``apt`` or ``docker pull``
  is still working.
* :meth:`SSHClient.start` — the same, but returns a :class:`RemoteProcess` the
  caller can stop early. Following a container's logs needs this.

All paths can escalate through ``sudo`` when the login user is not root, and all
are safe to call from a worker thread — one :class:`SSHClient` per thread, never
shared.
"""

import base64
import json
import shlex
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import paramiko

from core.credentials import KNOWN_HOSTS_FILE, config_dir
from core.distro import OSInfo, PackageManager
from models.host import Container, DockerStack

LineSink = Callable[[str], None]
"""Receives one line of remote output at a time, without its trailing newline."""

# Timeouts in seconds, sized to the slowest realistic case for each operation.
CONNECT_TIMEOUT = 10
REACHABILITY_TIMEOUT = 5
"""How long to spend deciding a host is simply offline, before trying to log in."""

KEEPALIVE_INTERVAL = 15
"""Keepalive period. Without it, a host that vanishes mid-command leaves the
worker thread blocked on a read that TCP will not fail for a very long time."""

PROBE_TIMEOUT = 15
FIND_TIMEOUT = 120
CHECK_UPDATES_TIMEOUT = 90
PULL_TIMEOUT = 1800
COMPOSE_UP_TIMEOUT = 600
COMPOSE_DOWN_TIMEOUT = 300
SYSTEM_UPDATE_TIMEOUT = 3600
VALIDATE_TIMEOUT = 60
PRUNE_TIMEOUT = 900
LOG_FOLLOW_TIMEOUT = 86400
"""Follow runs until the user stops it; the deadline is only a runaway guard."""

_POLL_INTERVAL = 0.2
_CHUNK_BYTES = 32768

_COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

#: Where compose projects normally live. Cheap to scan and covers the usual
#: layouts without walking the whole filesystem.
_SEARCH_ROOTS = ("/opt", "/srv", "/home", "/root", "/docker", "/stacks", "/data")

#: Never descend into these — either enormous, meaningless, or Docker's own
#: internal storage (which contains compose files belonging to no project).
_PRUNE_NAMES = ("node_modules", ".git", ".cache", "vendor")
_PRUNE_PATHS = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")

_MAX_STACKS = 250

# One container per line: project name, project directory, container state.
_PS_FORMAT = (
    '{{.Label "com.docker.compose.project"}}\t'
    '{{.Label "com.docker.compose.project.working_dir"}}\t'
    "{{.State}}"
)

#: Stack lifecycle actions, mapped to their compose arguments and a timeout.
#: ``down`` deliberately omits ``-v`` — removing volumes is data loss, not an
#: action a button should be able to take by accident.
COMPOSE_ACTIONS: dict[str, tuple[str, int]] = {
    "up": ("up -d", COMPOSE_UP_TIMEOUT),
    "down": ("down", COMPOSE_DOWN_TIMEOUT),
    "restart": ("restart", COMPOSE_UP_TIMEOUT),
    "stop": ("stop", COMPOSE_DOWN_TIMEOUT),
    "start": ("start", COMPOSE_UP_TIMEOUT),
    "pull": ("pull", PULL_TIMEOUT),
    "recreate": ("up -d --force-recreate", COMPOSE_UP_TIMEOUT),
}

#: Housekeeping targets. Each maps to a command and whether it can destroy data.
PRUNE_TARGETS: dict[str, tuple[str, str, bool]] = {
    "dangling-images": (
        "docker image prune -f",
        "Untagged images left behind by rebuilds",
        False,
    ),
    "unused-images": (
        "docker image prune -af",
        "Every image not used by an existing container",
        False,
    ),
    "stopped-containers": (
        "docker container prune -f",
        "Containers that have exited",
        False,
    ),
    "build-cache": ("docker builder prune -af", "Cached build layers", False),
    "unused-volumes": (
        "docker volume prune -f",
        "Volumes no container references — THIS DELETES DATA",
        True,
    ),
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a buffered remote command."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class RemoteProcess:
    """A command running on the host, whose output can be consumed or cut short.

    :meth:`pump` blocks on a worker thread forwarding output; :meth:`stop` can be
    called from any other thread — closing the channel makes the blocked read
    return, which is how the log viewer's Stop button works without leaving an
    orphaned ``docker compose logs -f`` behind.
    """

    def __init__(self, channel: paramiko.Channel):
        self._channel = channel
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        """Cancel the command. Safe to call repeatedly, and from any thread."""
        self._stopped = True
        try:
            self._channel.close()
        except (paramiko.SSHException, OSError):
            pass

    def pump(self, on_line: LineSink, timeout: int = SYSTEM_UPDATE_TIMEOUT) -> int:
        """Forward every line the command produces, then return its exit status.

        Returns ``0`` if the caller stopped it, and ``-1`` on timeout or a lost
        connection. Output is read with a short socket timeout rather than a
        busy-poll, so an idle command costs nothing while a chatty one is still
        forwarded promptly. stdout and stderr are interleaved into one stream,
        which is what makes the log read like a terminal session.
        """
        channel = self._channel
        channel.settimeout(_POLL_INTERVAL)
        partial = {"out": b"", "err": b""}

        def emit(stream: str, data: bytes) -> None:
            *lines, partial[stream] = (partial[stream] + data).split(b"\n")
            for raw in lines:
                text = raw.decode("utf-8", "replace").rstrip()
                # Suppress sudo's own prompt; the password went in via stdin.
                if text and not text.startswith("[sudo]"):
                    on_line(text)

        deadline = time.monotonic() + timeout
        stdout_open = True
        try:
            while not self._stopped:
                if time.monotonic() > deadline:
                    on_line(f"Timed out after {timeout}s — aborting.")
                    self.stop()
                    return -1

                if stdout_open:
                    try:
                        chunk = channel.recv(_CHUNK_BYTES)
                        if chunk:
                            emit("out", chunk)
                        else:
                            stdout_open = False
                    except socket.timeout:
                        pass

                while channel.recv_stderr_ready():
                    emit("err", channel.recv_stderr(_CHUNK_BYTES))

                if not stdout_open:
                    if channel.exit_status_ready():
                        break
                    time.sleep(_POLL_INTERVAL)

            # Flush anything left without a trailing newline.
            emit("out", b"\n")
            emit("err", b"\n")
            if self._stopped:
                return 0
            return channel.recv_exit_status()
        except (paramiko.SSHException, EOFError, OSError) as exc:
            if self._stopped:
                return 0  # The close() we asked for surfaced as a read error.
            on_line(f"Connection lost while running command: {exc}")
            return -1


class SSHClient:
    """A single SSH session to one host.

    Probe results that cannot change within a session (which compose binary
    exists, whether Docker needs ``sudo``, whether ``sudo`` needs a password)
    are cached after the first lookup, so a stack scan costs one round trip per
    fact rather than one per stack.
    """

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str = "",
        port: int = 22,
        key_path: str = "",
    ):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.key_path = key_path

        self._client: Optional[paramiko.SSHClient] = None
        self._compose_cmd: Optional[str] = None
        self._docker_sudo: Optional[bool] = None
        self._sudo_needs_password: Optional[bool] = None

    # ──────────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def connect(self, timeout: int = CONNECT_TIMEOUT) -> tuple[bool, str]:
        """Open the session. Returns ``(ok, message)`` — never raises.

        A plain TCP probe runs first so an offline host fails in seconds with a
        specific reason. Left to itself, paramiko tries every address
        ``getaddrinfo`` returns — each with the full timeout — so a dual-stack
        name that is simply down took multiples of ``timeout`` to give up.
        """
        unreachable = _reachability_error(
            self.hostname, self.port, min(timeout, REACHABILITY_TIMEOUT)
        )
        if unreachable:
            return False, unreachable

        client = paramiko.SSHClient()
        self._load_host_keys(client)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict = {
            "hostname": self.hostname,
            "port": self.port,
            "username": self.username,
            "timeout": timeout,
            "auth_timeout": timeout,
            "banner_timeout": timeout,
        }
        if self.key_path:
            kwargs["key_filename"] = self.key_path
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
            if self.password:
                # With a key configured, the stored secret is its passphrase.
                kwargs["passphrase"] = self.password
        elif self.password:
            kwargs["password"] = self.password
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        else:
            # Nothing configured — fall back to the agent and ~/.ssh defaults.
            kwargs["look_for_keys"] = True
            kwargs["allow_agent"] = True

        try:
            client.connect(**kwargs)
        except paramiko.BadHostKeyException as exc:
            return False, (
                f"Host key for {self.hostname} has changed since it was first "
                f"accepted (now {exc.key.get_base64()[:16]}…). This can mean the "
                f"server was rebuilt — or that the connection is being "
                f"intercepted. Remove the stale entry from "
                f"{KNOWN_HOSTS_FILE} to accept the new key."
            )
        except paramiko.AuthenticationException:
            hint = (
                "check the key file and its passphrase"
                if self.key_path
                else "check the username and password"
            )
            return False, f"Authentication failed — {hint}"
        except paramiko.NoValidConnectionsError:
            return False, f"Cannot reach {self.hostname}:{self.port}"
        except socket.timeout:
            return False, f"Connection timed out after {timeout}s"
        except (paramiko.SSHException, OSError) as exc:
            return False, str(exc) or exc.__class__.__name__

        transport = client.get_transport()
        if transport is not None:
            # Detect a host that disappears mid-command, instead of blocking on a
            # read until the OS eventually gives up on the TCP connection.
            transport.set_keepalive(KEEPALIVE_INTERVAL)

        self._client = client
        return True, "Connected"

    def disconnect(self) -> None:
        """Close the session. Safe to call more than once."""
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def __enter__(self) -> "SSHClient":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.disconnect()

    @staticmethod
    def _load_host_keys(client: paramiko.SSHClient) -> None:
        """Trust ``~/.ssh/known_hosts`` plus this app's own accepted-keys file.

        Loading a file (rather than only auto-adding in memory) is what lets
        paramiko save newly accepted keys and, crucially, raise
        :class:`~paramiko.BadHostKeyException` later if a host's key *changes*.
        """
        try:
            client.load_system_host_keys()
        except (OSError, paramiko.SSHException):
            pass
        try:
            config_dir()
            KNOWN_HOSTS_FILE.touch(exist_ok=True)
            client.load_host_keys(str(KNOWN_HOSTS_FILE))
        except (OSError, paramiko.SSHException):
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Command execution
    # ──────────────────────────────────────────────────────────────────────────

    def run(
        self,
        command: str,
        timeout: int = PROBE_TIMEOUT,
        sudo: bool = False,
        raw: bool = False,
    ) -> CommandResult:
        """Run ``command`` and return its buffered output. Never raises.

        ``raw`` keeps stdout exactly as received. Use it when reading a file —
        the default strips surrounding whitespace, which would corrupt content.
        """
        if self._client is None:
            return CommandResult(stderr="Not connected")

        prefix, feed_password = self._sudo_prefix() if sudo else ("", False)
        wrapped = f"{prefix}bash -c {shlex.quote(command)}" if prefix else command

        try:
            stdin, stdout, stderr = self._client.exec_command(wrapped, timeout=timeout)
            if feed_password:
                stdin.write(f"{self.password}\n")
                stdin.flush()
                stdin.channel.shutdown_write()
            # Read before reaping: recv_exit_status() has no timeout of its own,
            # so waiting on it first is what let a vanished host hang a worker
            # thread indefinitely. The reads honour the channel timeout, and by
            # the time they finish the status is already there.
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(
                stdout=out if raw else out.strip(),
                stderr=_without_sudo_prompt(err),
                exit_code=exit_code,
            )
        except (paramiko.SSHException, EOFError, OSError) as exc:
            return CommandResult(stderr=str(exc) or exc.__class__.__name__)

    def start(
        self, command: str, sudo: bool = False, timeout: int = SYSTEM_UPDATE_TIMEOUT
    ) -> tuple[Optional[RemoteProcess], str]:
        """Begin ``command``, returning a handle to it or an error message.

        The caller drives output with :meth:`RemoteProcess.pump` and can cancel
        with :meth:`RemoteProcess.stop`.
        """
        if self._client is None:
            return None, "Not connected."

        prefix, feed_password = self._sudo_prefix() if sudo else ("", False)
        wrapped = f"{prefix}bash -c {shlex.quote(command)}"

        try:
            stdin, stdout, _stderr = self._client.exec_command(wrapped, timeout=timeout)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            return None, f"Could not start command: {exc}"

        channel = stdout.channel
        try:
            if feed_password:
                stdin.write(f"{self.password}\n")
                stdin.flush()
            channel.shutdown_write()
        except (paramiko.SSHException, OSError):
            pass  # The remote may have exited before reading stdin.

        return RemoteProcess(channel), ""

    def stream(
        self,
        command: str,
        on_line: LineSink,
        timeout: int = SYSTEM_UPDATE_TIMEOUT,
        sudo: bool = False,
    ) -> int:
        """Run ``command`` to completion, reporting output as it arrives.

        Returns the remote exit status, or ``-1`` if the command could not be
        started, timed out, or the connection dropped mid-run.
        """
        process, error = self.start(command, sudo=sudo, timeout=timeout)
        if process is None:
            on_line(error)
            return -1
        return process.pump(on_line, timeout)

    def _sudo_prefix(self) -> tuple[str, bool]:
        """Return ``(command_prefix, must_feed_password)`` for privileged work.

        Passwordless ``sudo`` is probed once per session: when it is configured,
        the stored password is never sent over the wire at all.
        """
        if self.username == "root":
            return "", False
        if self._sudo_needs_password is None:
            self._sudo_needs_password = not self.run("sudo -n true", timeout=10).ok
        if not self._sudo_needs_password:
            return "sudo -n ", False
        return "sudo -S -p '' ", True

    # ──────────────────────────────────────────────────────────────────────────
    # Host inspection
    # ──────────────────────────────────────────────────────────────────────────

    def detect_os(self) -> OSInfo:
        """Identify the remote distribution from ``/etc/os-release``."""
        result = self.run("cat /etc/os-release")
        if result.ok and result.stdout:
            fields = self._parse_os_release(result.stdout)
            pretty = fields.get("PRETTY_NAME") or fields.get("NAME", "")
            if pretty:
                return OSInfo(
                    id=fields.get("ID", ""),
                    pretty=pretty,
                    like=fields.get("ID_LIKE", ""),
                )

        # No os-release (Alpine before 3.x, minimal images, BSDs): fall back to
        # something recognisable so the host is still usable.
        uname = self.run("uname -sr").stdout
        return OSInfo(pretty=uname or "Unknown OS")

    @staticmethod
    def _parse_os_release(text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip().strip("\"'")
        return fields

    def check_updates(self, manager: PackageManager) -> Optional[int]:
        """Count pending package updates. ``None`` when the count is unavailable.

        Reflects the package lists already on the host; it does not refresh
        them, since doing so requires root. Run a system update to resynchronise.
        """
        result = self.run(manager.check_cmd, timeout=CHECK_UPDATES_TIMEOUT)
        lines = result.stdout.splitlines()
        try:
            return max(0, int(lines[-1].strip()))
        except (ValueError, IndexError):
            return None

    def run_system_update(self, manager: PackageManager, on_line: LineSink) -> bool:
        """Apply all pending OS package updates. Returns True on success."""
        on_line(f"── System update via {manager.name} ──")
        if self.username != "root" and self.key_path and not self.password:
            on_line(
                "Note: this host authenticates with a key, so no sudo password "
                "is available. The update needs root — configure passwordless "
                "sudo, or log in as root."
            )

        exit_code = self.stream(
            manager.update_cmd,
            on_line,
            timeout=SYSTEM_UPDATE_TIMEOUT,
            sudo=True,
        )
        if exit_code == 0:
            on_line("System update completed.")
            return True
        on_line(f"System update failed (exit {exit_code}).")
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # Docker discovery
    # ──────────────────────────────────────────────────────────────────────────

    def find_docker_stacks(self, on_line: Optional[LineSink] = None) -> list[DockerStack]:
        """Discover Compose projects on the host.

        Two complementary sources, because neither alone is complete:

        * ``docker compose ls`` knows every project Docker has ever created,
          including its real project name — but not projects that were never
          started.
        * A filesystem scan finds compose files that have never been brought
          up — but has to guess the project name from the folder.

        Results are merged by directory, preferring Docker's own naming.
        """
        log = on_line or (lambda _msg: None)

        stacks: dict[str, DockerStack] = {}
        for project, compose_file in self._projects_known_to_docker():
            folder = _parent_dir(compose_file)
            if folder:
                stacks[folder] = DockerStack(
                    name=project or _basename(folder),
                    path=folder,
                    compose_file=compose_file,
                    project=project,
                )
        if stacks:
            log(f"Docker reports {len(stacks)} known project(s).")

        log("Scanning the filesystem for compose files…")
        for compose_file in self._find_compose_files(log):
            folder = _parent_dir(compose_file)
            if folder and folder not in stacks:
                stacks[folder] = DockerStack(
                    name=_basename(folder),
                    path=folder,
                    compose_file=compose_file,
                )

        found = [stacks[key] for key in sorted(stacks)]
        self._apply_stack_status(found)
        return found

    def _projects_known_to_docker(self) -> list[tuple[str, str]]:
        """Return ``(project_name, compose_file)`` from ``docker compose ls``."""
        compose = self._compose_command()
        if not compose:
            return []
        result = self.run(f"{self._sudo_n_prefix()}{compose} ls --all --format json")
        if not result.ok or not result.stdout:
            return []
        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(entries, list):
            return []

        projects: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # ConfigFiles is comma-separated when a project uses overrides;
            # the first entry is the base compose file.
            config = str(entry.get("ConfigFiles", "")).split(",")[0].strip()
            if config:
                projects.append((str(entry.get("Name", "")).strip(), config))
        return projects

    def _find_compose_files(self, log: LineSink) -> list[str]:
        """Scan the usual locations, widening to the whole tree only if empty."""
        result = self.run(self._find_command(_SEARCH_ROOTS, 6), timeout=FIND_TIMEOUT)
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if paths:
            return paths

        log("Nothing in the usual locations — scanning from / (slower)…")
        result = self.run(self._find_command(("/",), 7), timeout=FIND_TIMEOUT)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _find_command(roots: tuple[str, ...], max_depth: int) -> str:
        """Build a single pruning ``find`` for the compose filenames.

        Pruning matters: without it a scan wanders into ``node_modules`` and
        Docker's own overlay storage, which is both slow and full of compose
        files that belong to no project.
        """
        names = " -o ".join(f"-name {shlex.quote(n)}" for n in _COMPOSE_FILENAMES)
        skip = " -o ".join(
            [f"-name {shlex.quote(n)}" for n in _PRUNE_NAMES]
            + [f"-path {shlex.quote(p)}" for p in _PRUNE_PATHS]
        )
        return (
            f"find {' '.join(roots)} -maxdepth {max_depth} "
            rf"\( -type d \( {skip} \) -prune \) -o "
            rf"-type f \( {names} \) -print 2>/dev/null "
            f"| head -n {_MAX_STACKS} | sort -u"
        )

    def _apply_stack_status(self, stacks: list[DockerStack]) -> None:
        """Fill in each stack's ``status`` from the host's container list.

        Containers are matched on the Compose project directory as well as the
        project name, because a project renamed with ``-p`` or
        ``COMPOSE_PROJECT_NAME`` no longer matches its folder.
        """
        if not stacks:
            return
        result = self.run(
            f"{self._sudo_n_prefix()}docker ps --all --format {shlex.quote(_PS_FORMAT)}"
        )
        if not result.ok:
            return

        states_by_project: dict[str, list[str]] = {}
        states_by_dir: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            project, working_dir, state = (p.strip() for p in parts)
            if project:
                states_by_project.setdefault(project, []).append(state)
            if working_dir:
                states_by_dir.setdefault(working_dir.rstrip("/"), []).append(state)

        for stack in stacks:
            states = states_by_dir.get(stack.path.rstrip("/")) or states_by_project.get(
                stack.project or stack.name
            )
            if not states:
                stack.status = "stopped"
            elif all(state == "running" for state in states):
                stack.status = "running"
            elif any(state == "running" for state in states):
                stack.status = "partial"
            else:
                stack.status = "stopped"

    # ──────────────────────────────────────────────────────────────────────────
    # Docker operations
    # ──────────────────────────────────────────────────────────────────────────

    def update_stack(
        self,
        stack: DockerStack,
        on_line: LineSink,
        pull: bool = True,
    ) -> bool:
        """Pull the stack's images and recreate its containers.

        A failed pull aborts before ``up``, leaving the running stack alone —
        recreating containers against half-fetched images is how a routine
        update turns into an outage.
        """
        on_line(f"── Updating {stack.name} ({stack.path}) ──")
        if pull and not self.compose_action(stack, "pull", on_line):
            on_line(
                f"'{stack.name}' left running on its current images; "
                f"nothing was recreated."
            )
            return False
        return self.compose_action(stack, "up", on_line)

    def compose_action(self, stack: DockerStack, action: str, on_line: LineSink) -> bool:
        """Run one lifecycle action from :data:`COMPOSE_ACTIONS` against a stack."""
        entry = COMPOSE_ACTIONS.get(action)
        if entry is None:
            on_line(f"Unknown compose action '{action}'.")
            return False
        compose = self._compose_command()
        if not compose:
            on_line("Neither 'docker compose' nor 'docker-compose' is available.")
            return False

        args, timeout = entry
        on_line(f"{stack.name}: {compose} {args}")
        exit_code = self.stream(
            f"cd {shlex.quote(stack.path)} && {compose} {args}",
            on_line,
            timeout=timeout,
            sudo=self._docker_needs_sudo(),
        )
        if exit_code == 0:
            on_line(f"'{stack.name}': {action} completed.")
            return True
        on_line(f"'{stack.name}': {action} failed (exit {exit_code}).")
        return False

    def compose_ps(self, stack: DockerStack) -> list[Container]:
        """Containers belonging to one stack, running or not."""
        compose = self._compose_command()
        if not compose:
            return []
        result = self.run(
            f"cd {shlex.quote(stack.path)} && "
            f"{self._sudo_n_prefix()}{compose} ps --all --format json",
            timeout=PROBE_TIMEOUT,
        )
        if not result.ok:
            return []
        return _parse_compose_ps(result.stdout)

    def start_logs(
        self,
        stack: DockerStack,
        *,
        service: str = "",
        tail: "int | str" = 200,
        follow: bool = True,
        timestamps: bool = False,
    ) -> tuple[Optional[RemoteProcess], str]:
        """Begin streaming a stack's logs. Stop the returned process to end it.

        ``tail`` accepts a line count or the string ``"all"`` for full history.
        """
        compose = self._compose_command()
        if not compose:
            return None, "Neither 'docker compose' nor 'docker-compose' is available."

        history = "all" if str(tail) == "all" else str(max(0, _as_int(tail)))
        args = ["logs", "--no-color", f"--tail={history}"]
        if follow:
            args.append("--follow")
        if timestamps:
            args.append("--timestamps")
        if service:
            args.append(shlex.quote(service))

        return self.start(
            f"cd {shlex.quote(stack.path)} && {compose} {' '.join(args)}",
            sudo=self._docker_needs_sudo(),
            timeout=LOG_FOLLOW_TIMEOUT,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Compose file editing
    # ──────────────────────────────────────────────────────────────────────────

    def read_text(self, path: str) -> tuple[Optional[str], str]:
        """Read a remote text file. Returns ``(content, error_message)``.

        SFTP first, because it transfers bytes exactly and needs no shell
        quoting. A permission error falls back to ``cat`` and then ``sudo cat``,
        which is how a root-owned compose file under ``/opt`` is readable at all
        for a non-root login.
        """
        if self._client is None:
            return None, "Not connected."

        data, sftp_error = self._download(path)
        if data is not None:
            return data.decode("utf-8", "replace"), ""

        quoted = shlex.quote(path)
        plain = self.run(f"cat {quoted}", timeout=PROBE_TIMEOUT, raw=True)
        if plain.ok:
            return plain.stdout, ""
        escalated = self.run(f"cat {quoted}", timeout=PROBE_TIMEOUT, sudo=True, raw=True)
        if escalated.ok:
            return escalated.stdout, ""
        return None, escalated.stderr or plain.stderr or sftp_error

    def write_text(
        self, path: str, content: str, backup: bool = True
    ) -> tuple[bool, str]:
        """Replace a remote file's contents.

        Returns ``(ok, detail)`` where detail is the backup path on success, or
        the failure reason. The new content is staged in ``/tmp`` and copied over
        the target with ``cp``, which preserves the destination's owner and mode
        — writing the file afresh under ``sudo`` would silently change both.
        """
        if self._client is None:
            return False, "Not connected."

        quoted_target = shlex.quote(path)
        exists = self.run(f"test -f {quoted_target}").ok
        if exists:
            needs_sudo = not self.run(f"test -w {quoted_target}").ok
        else:
            needs_sudo = not self.run(f"test -w {shlex.quote(_parent_dir(path))}").ok

        staging = f"/tmp/.dsm-{uuid.uuid4().hex[:12]}.tmp"
        uploaded, error = self._upload(staging, content)
        if not uploaded:
            return False, error

        steps = []
        backup_path = ""
        if backup and exists:
            backup_path = f"{path}.bak.{datetime.now():%Y%m%d-%H%M%S}"
            steps.append(f"cp -p {quoted_target} {shlex.quote(backup_path)}")
        steps.append(f"cp {shlex.quote(staging)} {quoted_target}")

        result = self.run(
            " && ".join(steps), timeout=PROBE_TIMEOUT, sudo=needs_sudo
        )
        self.run(f"rm -f {shlex.quote(staging)}")

        if not result.ok:
            detail = result.stderr or f"exit {result.exit_code}"
            if needs_sudo:
                detail += " — this file needs root to modify"
            return False, detail
        return True, backup_path

    def validate_compose(self, stack: DockerStack, text: str) -> tuple[bool, str]:
        """Have Docker itself validate edited compose text.

        The text is staged in ``/tmp`` and checked with ``--project-directory``
        pointing at the stack, so relative paths, override files and ``.env``
        interpolation resolve exactly as they will at deploy time — without
        writing anything into the stack directory.
        """
        compose = self._compose_command()
        if not compose:
            return False, "Neither 'docker compose' nor 'docker-compose' is available."

        staging = f"/tmp/.dsm-validate-{uuid.uuid4().hex[:12]}.yml"
        uploaded, error = self._upload(staging, text)
        if not uploaded:
            return False, error

        result = self.run(
            f"{compose} --project-directory {shlex.quote(stack.path)} "
            f"-f {shlex.quote(staging)} config --quiet",
            timeout=VALIDATE_TIMEOUT,
        )
        self.run(f"rm -f {shlex.quote(staging)}")

        if result.ok:
            return True, "Docker accepted the file."
        # Compose puts the useful part of the complaint on stderr.
        detail = result.stderr or result.stdout or f"exit {result.exit_code}"
        return False, _strip_staging_path(detail, staging)

    def _download(self, path: str) -> tuple[Optional[bytes], str]:
        """Fetch a file over SFTP. Returns ``(data, error_message)``."""
        sftp = None
        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            handle = sftp.file(path, "rb")
            try:
                return handle.read(), ""
            finally:
                handle.close()
        except (paramiko.SSHException, EOFError, OSError) as exc:
            return None, str(exc) or exc.__class__.__name__
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except (paramiko.SSHException, OSError):
                    pass

    def _upload(self, path: str, content: str) -> tuple[bool, str]:
        """Write text to a remote path, falling back to base64 over the shell.

        The fallback exists for hosts with the SFTP subsystem disabled; base64
        keeps arbitrary content safe from the shell, which a heredoc would not.
        """
        payload = content.encode("utf-8")
        sftp = None
        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            handle = sftp.file(path, "wb")
            try:
                handle.write(payload)
            finally:
                handle.close()
            return True, ""
        except (paramiko.SSHException, EOFError, OSError):
            pass
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except (paramiko.SSHException, OSError):
                    pass

        encoded = base64.b64encode(payload).decode("ascii")
        result = self.run(
            f"printf '%s' {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}",
            timeout=PROBE_TIMEOUT,
        )
        if result.ok:
            return True, ""
        return False, result.stderr or f"could not write {path} (exit {result.exit_code})"

    # ──────────────────────────────────────────────────────────────────────────
    # Housekeeping
    # ──────────────────────────────────────────────────────────────────────────

    def disk_usage(self) -> str:
        """``docker system df`` output, or a message explaining why not."""
        result = self.run(
            f"{self._sudo_n_prefix()}docker system df", timeout=PROBE_TIMEOUT
        )
        if result.ok:
            return result.stdout
        return result.stderr or "Could not read Docker disk usage."

    def prune(self, targets: list[str], on_line: LineSink) -> bool:
        """Run the selected :data:`PRUNE_TARGETS` commands, in the order given."""
        if not targets:
            on_line("Nothing selected to clean up.")
            return False

        all_ok = True
        needs_sudo = self._docker_needs_sudo()
        for target in targets:
            entry = PRUNE_TARGETS.get(target)
            if entry is None:
                on_line(f"Unknown cleanup target '{target}'.")
                all_ok = False
                continue
            command, description, _destructive = entry
            on_line(f"── {description} ──")
            if self.stream(command, on_line, timeout=PRUNE_TIMEOUT, sudo=needs_sudo) != 0:
                all_ok = False
        return all_ok

    def _compose_command(self) -> str:
        """Return ``docker compose``, ``docker-compose``, or ``""`` if neither."""
        if self._compose_cmd is None:
            if self.run("docker compose version", timeout=PROBE_TIMEOUT).ok:
                self._compose_cmd = "docker compose"
            elif self.run("docker-compose --version", timeout=PROBE_TIMEOUT).ok:
                self._compose_cmd = "docker-compose"
            else:
                self._compose_cmd = ""
        return self._compose_cmd

    def _docker_needs_sudo(self) -> bool:
        """Whether Docker commands must be escalated for this login user.

        Users outside the ``docker`` group cannot reach the daemon socket, so
        probing once here is what lets the same code serve both setups.
        """
        if self._docker_sudo is None:
            if self.username == "root":
                self._docker_sudo = False
            else:
                self._docker_sudo = not self.run("docker info", timeout=PROBE_TIMEOUT).ok
        return self._docker_sudo

    def _sudo_n_prefix(self) -> str:
        """``sudo -n `` for read-only Docker probes, or ``""``.

        Probes run through the buffered :meth:`run` path, which has nowhere to
        send a password — so they only escalate when it is free to do so.
        """
        if not self._docker_needs_sudo():
            return ""
        prefix, needs_password = self._sudo_prefix()
        return "" if needs_password else prefix


def _parent_dir(path: str) -> str:
    """Directory portion of an absolute POSIX path (``os.path`` is local-flavoured)."""
    folder = path.rsplit("/", 1)[0]
    return folder or "/"


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _reachability_error(hostname: str, port: int, timeout: int) -> str:
    """Return why ``hostname:port`` cannot be reached, or ``""`` if it can.

    ``timeout`` is a budget for the whole check, not per address. That
    distinction is the point: a name resolving to both an IPv6 and an IPv4
    address costs *twice* the timeout with :func:`socket.create_connection`,
    which is measurable — a refused connection to ``localhost`` takes twice as
    long as the same connection to ``127.0.0.1``.

    A refused connection ends the check immediately: the host answered, so it is
    up, and trying its other addresses cannot change the diagnosis.

    Name resolution itself is not bounded — the standard library offers no way to
    time out :func:`socket.getaddrinfo` — so this must be called off the UI
    thread.
    """
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return f"Cannot resolve '{hostname}' — check the hostname or your DNS."
    except OSError as exc:
        return f"Cannot look up '{hostname}' — {_reason(exc)}."
    if not addresses:
        return f"Cannot resolve '{hostname}' — it has no addresses."

    offline = (
        f"No answer from {hostname}:{port} within {timeout}s — "
        f"the host appears to be offline."
    )
    deadline = time.monotonic() + timeout
    last_error = ""

    for index, (family, socket_type, proto, _canonical, address) in enumerate(addresses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Share what is left of the budget among the addresses still untried.
        sock = socket.socket(family, socket_type, proto)
        try:
            sock.settimeout(max(1.0, remaining / (len(addresses) - index)))
            sock.connect(address)
            return ""
        except ConnectionRefusedError:
            return f"{hostname}:{port} refused the connection — is SSH running there?"
        except (socket.timeout, TimeoutError):
            last_error = offline
        except OSError as exc:
            last_error = f"Cannot reach {hostname}:{port} — {_reason(exc)}."
        finally:
            sock.close()

    return last_error or offline


def _reason(exc: OSError) -> str:
    return getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__


def _without_sudo_prompt(text: str) -> str:
    """Drop sudo's password prompt from stderr so it never reaches the user."""
    kept = [
        line
        for line in text.splitlines()
        if line.strip() and not line.startswith("[sudo]")
    ]
    return "\n".join(kept).strip()


def _strip_staging_path(text: str, staging: str) -> str:
    """Replace the temp filename in Docker's complaint with something meaningful.

    Compose reports errors against the file it was given, and telling the user
    about ``/tmp/.dsm-validate-9f2c.yml`` is worse than telling them nothing.
    """
    return text.replace(staging, "the edited file")


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_compose_ps(payload: str) -> list[Container]:
    """Parse ``docker compose ps --format json``, whose shape changed by version.

    Compose 2.21 and later print a JSON array; earlier versions print one object
    per line. Both are accepted, and anything unparsable is skipped rather than
    failing the whole listing.
    """
    text = payload.strip()
    if not text:
        return []

    entries: list = []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            entries = parsed
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    containers = [
        Container(
            name=str(entry.get("Name", "")),
            service=str(entry.get("Service", "")),
            state=str(entry.get("State", "")).lower(),
            health=str(entry.get("Health", "")).lower(),
            image=str(entry.get("Image", "")),
            ports=_format_publishers(entry.get("Publishers")),
            exit_code=_as_int(entry.get("ExitCode")),
        )
        for entry in entries
        if isinstance(entry, dict)
    ]
    containers.sort(key=lambda container: (container.service, container.name))
    return containers


def _format_publishers(publishers: object) -> str:
    """Condense the ``Publishers`` array into ``8080->80/tcp`` display text."""
    if not isinstance(publishers, list):
        return ""
    parts: list[str] = []
    for publisher in publishers:
        if not isinstance(publisher, dict):
            continue
        published = _as_int(publisher.get("PublishedPort"))
        if not published:
            continue  # 0 means the port is not published to the host.
        target = _as_int(publisher.get("TargetPort"))
        protocol = str(publisher.get("Protocol") or "tcp")
        text = f"{published}->{target}/{protocol}"
        if text not in parts:
            parts.append(text)
    return ", ".join(parts)
