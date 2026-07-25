"""SSH transport: connect to a host, inspect it, and run commands on it.

This module is deliberately distro-agnostic — it knows how to *run* a command
and hand its output back, while :mod:`core.distro` decides *which* commands to
run. There are two execution paths:

* :meth:`SSHClient.run` — one-shot command, fully buffered. For fast probes.
* :meth:`SSHClient.stream` — long-running command whose output is delivered a
  line at a time, so the GUI can show progress while ``apt`` or ``docker pull``
  is still working.

Both paths can escalate through ``sudo`` when the login user is not root, and
both are safe to call from a worker thread — one :class:`SSHClient` per thread,
never shared.
"""

import json
import shlex
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

import paramiko

from core.credentials import KNOWN_HOSTS_FILE, config_dir
from core.distro import OSInfo, PackageManager
from models.host import DockerStack

LineSink = Callable[[str], None]
"""Receives one line of remote output at a time, without its trailing newline."""

# Timeouts in seconds, sized to the slowest realistic case for each operation.
CONNECT_TIMEOUT = 10
PROBE_TIMEOUT = 15
FIND_TIMEOUT = 120
CHECK_UPDATES_TIMEOUT = 90
PULL_TIMEOUT = 1800
COMPOSE_UP_TIMEOUT = 600
SYSTEM_UPDATE_TIMEOUT = 3600

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


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a buffered remote command."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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
        """Open the session. Returns ``(ok, message)`` — never raises."""
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

    def run(self, command: str, timeout: int = PROBE_TIMEOUT) -> CommandResult:
        """Run ``command`` and return its buffered output. Never raises."""
        if self._client is None:
            return CommandResult(stderr="Not connected")
        try:
            _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(
                stdout=stdout.read().decode("utf-8", "replace").strip(),
                stderr=stderr.read().decode("utf-8", "replace").strip(),
                exit_code=exit_code,
            )
        except (paramiko.SSHException, socket.error, EOFError) as exc:
            return CommandResult(stderr=str(exc) or exc.__class__.__name__)

    def stream(
        self,
        command: str,
        on_line: LineSink,
        timeout: int = SYSTEM_UPDATE_TIMEOUT,
        sudo: bool = False,
    ) -> int:
        """Run ``command``, reporting output to ``on_line`` as it arrives.

        Returns the remote exit status, or ``-1`` if the command could not be
        started, timed out, or the connection failed mid-run.

        Output is read with a short socket timeout rather than a busy-poll, so
        an idle command costs nothing while a chatty one is still forwarded
        promptly. stdout and stderr are interleaved into one stream, which is
        what makes the log read like a terminal session.
        """
        if self._client is None:
            on_line("Not connected.")
            return -1

        prefix, feed_password = self._sudo_prefix() if sudo else ("", False)
        wrapped = f"{prefix}bash -c {shlex.quote(command)}"

        try:
            stdin, stdout, _stderr = self._client.exec_command(wrapped, timeout=timeout)
        except (paramiko.SSHException, socket.error) as exc:
            on_line(f"Could not start command: {exc}")
            return -1

        channel = stdout.channel
        try:
            if feed_password:
                stdin.write(f"{self.password}\n")
                stdin.flush()
            channel.shutdown_write()
        except (paramiko.SSHException, socket.error, OSError):
            pass  # The remote may have exited before reading stdin.

        return self._pump(channel, on_line, timeout)

    @staticmethod
    def _pump(channel: paramiko.Channel, on_line: LineSink, timeout: int) -> int:
        """Forward everything ``channel`` produces to ``on_line``, then reap it."""
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
            while True:
                if time.monotonic() > deadline:
                    on_line(f"Timed out after {timeout}s — aborting.")
                    channel.close()
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
            return channel.recv_exit_status()
        except (paramiko.SSHException, socket.error, EOFError) as exc:
            on_line(f"Connection lost while running command: {exc}")
            return -1

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
        compose = self._compose_command()
        if not compose:
            on_line("Neither 'docker compose' nor 'docker-compose' is available.")
            return False

        on_line(f"── {stack.name} ({stack.path}) ──")
        needs_sudo = self._docker_needs_sudo()
        in_dir = f"cd {shlex.quote(stack.path)} && {compose}"

        if pull:
            on_line("Pulling latest images…")
            exit_code = self.stream(
                f"{in_dir} pull", on_line, timeout=PULL_TIMEOUT, sudo=needs_sudo
            )
            if exit_code != 0:
                on_line(
                    f"Pull failed (exit {exit_code}) — '{stack.name}' left running "
                    f"on its current images."
                )
                return False

        on_line("Recreating containers…")
        exit_code = self.stream(
            f"{in_dir} up -d", on_line, timeout=COMPOSE_UP_TIMEOUT, sudo=needs_sudo
        )
        if exit_code == 0:
            on_line(f"'{stack.name}' updated.")
            return True
        on_line(f"'{stack.name}' failed to start (exit {exit_code}).")
        return False

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
