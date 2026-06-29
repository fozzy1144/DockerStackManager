import shlex
import socket
import time
import paramiko
from typing import Callable, Optional
from models.host import DockerStack


class SSHClient:
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
        self._compose_bin: str = ""

    def connect(self, timeout: int = 10) -> tuple[bool, str]:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict = dict(
                hostname=self.hostname,
                port=self.port,
                username=self.username,
                timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            if self.key_path:
                connect_kwargs["key_filename"] = self.key_path
                if self.password:
                    connect_kwargs["passphrase"] = self.password
            else:
                connect_kwargs["password"] = self.password

            client.connect(**connect_kwargs)
            self._client = client
            return True, "Connected"
        except paramiko.AuthenticationException:
            hint = "check key file / passphrase" if self.key_path else "check username/password"
            return False, f"Authentication failed — {hint}"
        except paramiko.NoValidConnectionsError:
            return False, f"Cannot connect to {self.hostname}:{self.port}"
        except socket.timeout:
            return False, f"Connection timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

    def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None

    def run(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        if not self._client:
            return "", "Not connected", -1
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return out, err, exit_code
        except Exception as e:
            return "", str(e), -1

    def detect_os(self) -> tuple[str, str]:
        out, _, code = self.run("cat /etc/os-release 2>/dev/null || uname -a")
        if code != 0 or not out:
            return "linux", "Linux (unknown)"

        fields = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip().strip('"')

        os_id = fields.get("ID", "linux")
        pretty = fields.get("PRETTY_NAME", fields.get("NAME", ""))

        if not pretty or "uname" in out.lower():
            uname, _, _ = self.run("uname -sr")
            pretty = uname or "Linux"

        return os_id, pretty

    def find_docker_stacks(self, log_cb: Callable[[str], None] | None = None) -> list[DockerStack]:
        if log_cb:
            log_cb("Scanning for docker compose files...")

        search_cmd = (
            "find /opt /home /srv /root /var/lib/docker /docker /stack /compose "
            r"-maxdepth 6 \( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
            r"-o -name 'compose.yml' -o -name 'compose.yaml' \) 2>/dev/null | sort"
        )
        out, _, _ = self.run(search_cmd, timeout=60)

        if not out:
            out, _, _ = self.run(
                r"find / -maxdepth 8 \( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
                r"-o -name 'compose.yml' -o -name 'compose.yaml' \) 2>/dev/null | head -100 | sort",
                timeout=90,
            )

        stacks: list[DockerStack] = []
        seen_paths: set[str] = set()

        for compose_file in out.splitlines():
            compose_file = compose_file.strip()
            if not compose_file:
                continue
            folder = compose_file.rsplit("/", 1)[0]
            if folder in seen_paths:
                continue
            seen_paths.add(folder)
            name = folder.rsplit("/", 1)[-1]
            stacks.append(DockerStack(name=name, path=folder, compose_file=compose_file))

        if log_cb:
            log_cb(f"Found {len(stacks)} stack(s)")

        self._populate_stack_status(stacks)
        return stacks

    def _populate_stack_status(self, stacks: list[DockerStack]):
        ps_out, _, code = self.run(
            "docker ps --format '{{.Label \"com.docker.compose.project\"}}|{{.State}}' 2>/dev/null",
            timeout=15,
        )
        running_projects: dict[str, set[str]] = {}
        if code == 0:
            for line in ps_out.splitlines():
                if "|" in line:
                    proj, state = line.split("|", 1)
                    proj = proj.strip()
                    if proj:
                        running_projects.setdefault(proj, set()).add(state.strip())

        for stack in stacks:
            project_states = running_projects.get(stack.name)
            if project_states is None:
                stack.status = "stopped"
            elif all(s == "running" for s in project_states):
                stack.status = "running"
            else:
                stack.status = "partial"

    def update_stack(
        self,
        stack: DockerStack,
        log_cb: Callable[[str], None],
        pull: bool = True,
    ) -> bool:
        compose_bin = self._get_compose_bin()
        log_cb(f"\n--- Updating {stack.name} ({stack.path}) ---")

        if pull:
            log_cb("Pulling latest images...")
            out, _, pull_code = self.run(
                f"cd {stack.path} && {compose_bin} pull 2>&1",
                timeout=300,
            )
            for line in out.splitlines():
                log_cb(line)
            if pull_code != 0:
                log_cb(f"Pull failed (exit {pull_code}) — skipping up.")
                return False

        log_cb("Bringing stack up...")
        out, _, up_code = self.run(
            f"cd {stack.path} && {compose_bin} up -d 2>&1",
            timeout=120,
        )
        for line in out.splitlines():
            log_cb(line)

        if up_code == 0:
            log_cb(f"Stack '{stack.name}' updated successfully.")
            return True
        log_cb(f"Stack '{stack.name}' update failed (exit {up_code}).")
        return False

    def run_system_update(self, os_id: str, log_cb: Callable[[str], None]) -> bool:
        log_cb("\n--- Running system update ---")
        if os_id in ("ubuntu", "debian", "raspbian", "linuxmint", "pop"):
            cmd = "DEBIAN_FRONTEND=noninteractive apt-get update -y && apt-get upgrade -y"
        elif os_id in ("fedora", "centos", "rhel", "rocky", "almalinux"):
            cmd = "dnf upgrade -y || yum upgrade -y"
        elif os_id in ("arch", "manjaro", "endeavouros"):
            cmd = "pacman -Syu --noconfirm"
        elif os_id == "alpine":
            cmd = "apk update && apk upgrade"
        elif os_id in ("opensuse", "suse", "opensuse-leap", "opensuse-tumbleweed"):
            cmd = "zypper refresh && zypper update -y"
        else:
            log_cb(f"Unknown OS '{os_id}' — attempting apt-get then dnf...")
            cmd = "apt-get update -y && apt-get upgrade -y || dnf upgrade -y"

        exit_code = self._run_sudo_streaming(cmd, log_cb, timeout=600)
        success = exit_code == 0
        log_cb("System update " + ("completed successfully." if success else f"failed (exit {exit_code})."))
        return success

    def _run_sudo_streaming(self, command: str, log_cb: Callable[[str], None], timeout: int = 600) -> int:
        if not self._client:
            return -1
        try:
            if self.username == "root":
                wrapped = f"bash -c {shlex.quote(command)}"
            else:
                wrapped = f"sudo -S -p '' bash -c {shlex.quote(command)}"

            stdin, stdout, stderr = self._client.exec_command(wrapped, timeout=timeout)
            if self.username != "root":
                stdin.write(self.password + "\n")
                stdin.flush()
            stdin.channel.shutdown_write()

            stdout_buf = b""
            stderr_buf = b""

            while not stdout.channel.exit_status_ready():
                if stdout.channel.recv_ready():
                    chunk = stdout.channel.recv(4096)
                    stdout_buf += chunk
                    lines = stdout_buf.split(b"\n")
                    stdout_buf = lines[-1]
                    for line in lines[:-1]:
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if text:
                            log_cb(text)
                if stdout.channel.recv_stderr_ready():
                    chunk = stdout.channel.recv_stderr(4096)
                    stderr_buf += chunk
                    lines = stderr_buf.split(b"\n")
                    stderr_buf = lines[-1]
                    for line in lines[:-1]:
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if text and not text.startswith("[sudo]"):
                            log_cb(text)
                time.sleep(0.05)

            tail = (
                stdout_buf.decode("utf-8", errors="replace")
                + stdout.read().decode("utf-8", errors="replace")
                + stderr_buf.decode("utf-8", errors="replace")
                + stderr.read().decode("utf-8", errors="replace")
            )
            for line in tail.splitlines():
                if line.strip() and not line.startswith("[sudo]"):
                    log_cb(line)

            return stdout.channel.recv_exit_status()
        except Exception as e:
            log_cb(f"Error: {e}")
            return -1

    def _get_compose_bin(self) -> str:
        if self._compose_bin:
            return self._compose_bin
        out, _, code = self.run("docker compose version 2>/dev/null", timeout=5)
        if code == 0:
            self._compose_bin = "docker compose"
        else:
            _, _, code2 = self.run("which docker-compose 2>/dev/null", timeout=5)
            self._compose_bin = "docker-compose" if code2 == 0 else "docker compose"
        return self._compose_bin

    @property
    def is_connected(self) -> bool:
        if not self._client:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()
