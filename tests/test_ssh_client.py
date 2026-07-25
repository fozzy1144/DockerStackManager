"""Tests for the SSH layer's pure logic and its failure handling.

Nothing here needs a real host: command construction, output parsing, and the
reachability probe are all testable locally, and a fake channel stands in for
paramiko where a live session would otherwise be required.
"""

import json
import socket
import time
import unittest

from core import distro
from core.ssh_client import (
    COMPOSE_ACTIONS,
    PRUNE_TARGETS,
    REACHABILITY_TIMEOUT,
    CommandResult,
    SSHClient,
    _basename,
    _format_publishers,
    _parent_dir,
    _parse_compose_ps,
    _reachability_error,
    _strip_staging_path,
    _without_sudo_prompt,
)
from models.host import DockerStack


class FakeSSH(SSHClient):
    """An SSHClient whose ``run`` replays canned results and records commands."""

    def __init__(self, responses=None):
        super().__init__("host", "root")
        self._responses = responses or {}
        self.commands: list[str] = []

    def run(self, command, timeout=15, sudo=False, raw=False):
        self.commands.append(command)
        for needle, result in self._responses.items():
            if needle in command:
                return result
        return CommandResult(stderr="no match", exit_code=1)

    def ran(self, fragment: str) -> bool:
        return any(fragment in command for command in self.commands)


class TestReachabilityProbe(unittest.TestCase):
    """Regression tests for the hang when a host is offline.

    The symptom was an apparently frozen window: paramiko tried every address a
    name resolved to, each with the full timeout, before reporting anything.
    """

    @staticmethod
    def _closed_port() -> int:
        """A port on loopback with nothing listening."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_refused_port_is_reported_as_refused(self):
        started = time.monotonic()
        error = _reachability_error("127.0.0.1", self._closed_port(), REACHABILITY_TIMEOUT)
        elapsed = time.monotonic() - started

        self.assertTrue(error)
        self.assertIn("refused", error.lower())
        # Windows takes ~2s to surface WSAECONNREFUSED even on loopback; the
        # budget must still cap the whole check.
        self.assertLess(elapsed, REACHABILITY_TIMEOUT + 2)

    def test_budget_is_not_multiplied_by_address_count(self):
        """The regression this probe exists for.

        ``localhost`` resolves to both ::1 and 127.0.0.1. Connecting with
        :func:`socket.create_connection` spends the full timeout on each, so the
        check must not simply delegate to it.
        """
        started = time.monotonic()
        error = _reachability_error("localhost", self._closed_port(), REACHABILITY_TIMEOUT)
        elapsed = time.monotonic() - started

        self.assertTrue(error)
        self.assertLess(
            elapsed,
            REACHABILITY_TIMEOUT + 2,
            f"took {elapsed:.1f}s — the budget is being applied per address",
        )

    def test_unresolvable_name_is_reported(self):
        error = _reachability_error("no-such-host.invalid", 22, REACHABILITY_TIMEOUT)
        self.assertTrue(error)
        self.assertIn("resolve", error.lower())

    def test_offline_host_gives_up_within_the_timeout(self):
        # 192.0.2.0/24 is TEST-NET-1: reserved, and routed nowhere.
        timeout = 3
        started = time.monotonic()
        error = _reachability_error("192.0.2.1", 22, timeout)
        elapsed = time.monotonic() - started

        self.assertTrue(error, "an unroutable address must produce an error")
        self.assertLess(
            elapsed, timeout + 3, f"probe took {elapsed:.1f}s for a {timeout}s budget"
        )

    def test_reachable_port_returns_no_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            self.assertEqual(_reachability_error("127.0.0.1", port, 5), "")
        finally:
            listener.close()

    def test_connect_fails_fast_without_touching_paramiko(self):
        client = SSHClient("no-such-host.invalid", "root", "pw")
        ok, message = client.connect(timeout=5)
        self.assertFalse(ok)
        self.assertIn("resolve", message.lower())
        self.assertFalse(client.is_connected)


class TestComposePsParsing(unittest.TestCase):
    def test_json_array_form(self):
        payload = json.dumps(
            [
                {
                    "Name": "app-web-1",
                    "Service": "web",
                    "State": "running",
                    "Health": "healthy",
                    "Image": "nginx:1.27",
                    "Publishers": [
                        {"PublishedPort": 8080, "TargetPort": 80, "Protocol": "tcp"}
                    ],
                }
            ]
        )
        containers = _parse_compose_ps(payload)
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0].service, "web")
        self.assertEqual(containers[0].status_label, "running (healthy)")
        self.assertEqual(containers[0].ports, "8080->80/tcp")
        self.assertTrue(containers[0].is_running)

    def test_ndjson_form_from_older_compose(self):
        payload = (
            '{"Name":"a","Service":"zulu","State":"exited","ExitCode":137}\n'
            '{"Name":"b","Service":"alpha","State":"running"}\n'
        )
        containers = _parse_compose_ps(payload)
        self.assertEqual([c.service for c in containers], ["alpha", "zulu"])
        self.assertEqual(containers[1].status_label, "exited (137)")

    def test_unpublished_ports_are_omitted(self):
        payload = json.dumps(
            [{"Name": "a", "Service": "a", "State": "running",
              "Publishers": [{"PublishedPort": 0, "TargetPort": 443}]}]
        )
        self.assertEqual(_parse_compose_ps(payload)[0].ports, "")

    def test_duplicate_publishers_are_collapsed(self):
        publishers = [
            {"PublishedPort": 80, "TargetPort": 80, "Protocol": "tcp"},
            {"PublishedPort": 80, "TargetPort": 80, "Protocol": "tcp"},
        ]
        self.assertEqual(_format_publishers(publishers), "80->80/tcp")

    def test_malformed_input_is_ignored(self):
        for payload in ("", "   ", "not json", "[", '{"broken":', "[1, 2, 3]", "null"):
            self.assertEqual(_parse_compose_ps(payload), [])

    def test_partial_ndjson_keeps_the_good_lines(self):
        payload = '{"Name":"ok","Service":"ok","State":"running"}\ngarbage\n'
        self.assertEqual(len(_parse_compose_ps(payload)), 1)

    def test_non_list_publishers(self):
        self.assertEqual(_format_publishers(None), "")
        self.assertEqual(_format_publishers("nope"), "")


class TestCommandConstruction(unittest.TestCase):
    def setUp(self):
        self.stack = DockerStack("app", "/opt/my app", "/opt/my app/docker-compose.yml")

    def test_compose_ps_quotes_the_path(self):
        ssh = FakeSSH({
            "docker compose version": CommandResult(exit_code=0),
            "ps --all": CommandResult(stdout="[]", exit_code=0),
        })
        ssh._docker_sudo = False
        ssh.compose_ps(self.stack)
        self.assertTrue(ssh.ran("cd '/opt/my app'"), ssh.commands)

    def test_unknown_action_is_refused(self):
        ssh = FakeSSH()
        lines: list[str] = []
        self.assertFalse(ssh.compose_action(self.stack, "explode", lines.append))
        self.assertTrue(any("Unknown compose action" in line for line in lines))

    def test_every_action_has_a_timeout(self):
        for action, (args, timeout) in COMPOSE_ACTIONS.items():
            self.assertTrue(args, action)
            self.assertGreater(timeout, 0, action)

    def test_down_never_removes_volumes(self):
        # Guard against someone "improving" this into data loss.
        args, _ = COMPOSE_ACTIONS["down"]
        self.assertNotIn("-v", args.split())
        self.assertNotIn("--volumes", args)

    def test_prune_targets_are_well_formed(self):
        for key, (command, description, destructive) in PRUNE_TARGETS.items():
            self.assertIn("prune", command, key)
            # Must not stop for confirmation: -f, or -af where 'a' widens scope.
            self.assertRegex(command, r"-a?f\b", key)
            self.assertTrue(description, key)
            self.assertIsInstance(destructive, bool)

    def test_only_volume_pruning_is_marked_destructive(self):
        destructive = {k for k, v in PRUNE_TARGETS.items() if v[2]}
        self.assertEqual(destructive, {"unused-volumes"})

    def test_prune_rejects_unknown_target(self):
        ssh = FakeSSH()
        lines: list[str] = []
        self.assertFalse(ssh.prune(["nonsense"], lines.append))
        self.assertTrue(any("Unknown cleanup target" in line for line in lines))

    def test_prune_with_nothing_selected(self):
        lines: list[str] = []
        self.assertFalse(FakeSSH().prune([], lines.append))


class TestUpdateChecks(unittest.TestCase):
    def test_count_is_parsed(self):
        ssh = FakeSSH({"apt-get": CommandResult(stdout="12", exit_code=0)})
        self.assertEqual(ssh.check_updates(distro.APT), 12)

    def test_zero_with_nonzero_exit(self):
        # `grep -c` exits 1 when the count is zero; the number still matters.
        ssh = FakeSSH({"apt-get": CommandResult(stdout="0", exit_code=1)})
        self.assertEqual(ssh.check_updates(distro.APT), 0)

    def test_last_line_wins(self):
        ssh = FakeSSH({"apt-get": CommandResult(stdout="warning: x\n5", exit_code=0)})
        self.assertEqual(ssh.check_updates(distro.APT), 5)

    def test_unparsable_is_none(self):
        for output in ("", "nope", "\n"):
            ssh = FakeSSH({"apt-get": CommandResult(stdout=output, exit_code=0)})
            self.assertIsNone(ssh.check_updates(distro.APT))

    def test_negative_counts_are_clamped(self):
        ssh = FakeSSH({"apt-get": CommandResult(stdout="-3", exit_code=0)})
        self.assertEqual(ssh.check_updates(distro.APT), 0)


class TestSudoDecisions(unittest.TestCase):
    def test_root_never_escalates(self):
        ssh = FakeSSH()
        ssh.username = "root"
        self.assertEqual(ssh._sudo_prefix(), ("", False))
        self.assertFalse(ssh._docker_needs_sudo())

    def test_passwordless_sudo_avoids_sending_the_password(self):
        ssh = FakeSSH({"sudo -n true": CommandResult(exit_code=0)})
        ssh.username = "ev"
        prefix, needs_password = ssh._sudo_prefix()
        self.assertEqual(prefix, "sudo -n ")
        self.assertFalse(needs_password)

    def test_password_sudo_when_nopasswd_is_unavailable(self):
        ssh = FakeSSH()
        ssh.username = "ev"
        prefix, needs_password = ssh._sudo_prefix()
        self.assertIn("sudo -S", prefix)
        self.assertTrue(needs_password)

    def test_probe_result_is_cached(self):
        ssh = FakeSSH({"sudo -n true": CommandResult(exit_code=0)})
        ssh.username = "ev"
        ssh._sudo_prefix()
        before = len(ssh.commands)
        ssh._sudo_prefix()
        self.assertEqual(len(ssh.commands), before)

    def test_buffered_probes_do_not_escalate_when_a_password_is_needed(self):
        ssh = FakeSSH()
        ssh.username = "ev"
        ssh._docker_sudo = True
        self.assertEqual(ssh._sudo_n_prefix(), "")

    def test_compose_command_detection_prefers_the_plugin(self):
        ssh = FakeSSH({"docker compose version": CommandResult(exit_code=0)})
        self.assertEqual(ssh._compose_command(), "docker compose")

    def test_compose_command_falls_back_to_v1(self):
        ssh = FakeSSH({"docker-compose --version": CommandResult(exit_code=0)})
        self.assertEqual(ssh._compose_command(), "docker-compose")

    def test_compose_command_absent(self):
        self.assertEqual(FakeSSH()._compose_command(), "")


class TestNotConnected(unittest.TestCase):
    """Every entry point must degrade rather than raise when there is no session."""

    def setUp(self):
        self.ssh = SSHClient("host", "user")
        self.stack = DockerStack("a", "/opt/a", "/opt/a/compose.yml")

    def test_run(self):
        self.assertFalse(self.ssh.run("ls").ok)

    def test_read_text(self):
        content, error = self.ssh.read_text("/opt/a/compose.yml")
        self.assertIsNone(content)
        self.assertTrue(error)

    def test_write_text(self):
        ok, detail = self.ssh.write_text("/opt/a/compose.yml", "x")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_start(self):
        process, error = self.ssh.start("ls")
        self.assertIsNone(process)
        self.assertTrue(error)

    def test_stream(self):
        lines: list[str] = []
        self.assertEqual(self.ssh.stream("ls", lines.append), -1)
        self.assertTrue(lines)

    def test_is_connected(self):
        self.assertFalse(self.ssh.is_connected)

    def test_disconnect_is_safe(self):
        self.ssh.disconnect()
        self.ssh.disconnect()

    def test_context_manager(self):
        with SSHClient("h", "u") as client:
            self.assertFalse(client.is_connected)


class TestHelpers(unittest.TestCase):
    def test_parent_dir(self):
        self.assertEqual(_parent_dir("/opt/a/compose.yml"), "/opt/a")
        self.assertEqual(_parent_dir("/compose.yml"), "/")

    def test_basename(self):
        self.assertEqual(_basename("/opt/a/"), "a")
        self.assertEqual(_basename("/opt/abc"), "abc")

    def test_sudo_prompt_is_stripped(self):
        self.assertEqual(
            _without_sudo_prompt("[sudo] password for ev: \nreal error\n"), "real error"
        )

    def test_staging_path_is_hidden_from_errors(self):
        message = _strip_staging_path("/tmp/.dsm-abc.yml: bad key", "/tmp/.dsm-abc.yml")
        self.assertNotIn(".dsm-", message)
        self.assertIn("the edited file", message)

    def test_os_release_parsing(self):
        fields = SSHClient._parse_os_release(
            'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
            "ID=debian\nID_LIKE=\nNO_EQUALS_HERE\n"
        )
        self.assertEqual(fields["ID"], "debian")
        self.assertEqual(fields["PRETTY_NAME"], "Debian GNU/Linux 12 (bookworm)")
        self.assertNotIn("NO_EQUALS_HERE", fields)

    def test_find_command_prunes_and_limits(self):
        command = SSHClient._find_command(("/opt", "/srv"), 6)
        self.assertIn("-maxdepth 6", command)
        self.assertIn("node_modules", command)
        self.assertIn("/var/lib/docker", command)
        self.assertIn("docker-compose.yml", command)
        self.assertIn("compose.yaml", command)
        self.assertIn("head -n", command)

    def test_command_result_ok(self):
        self.assertTrue(CommandResult(exit_code=0).ok)
        self.assertFalse(CommandResult(exit_code=1).ok)
        self.assertFalse(CommandResult().ok)


class TestStackStatus(unittest.TestCase):
    def _apply(self, ps_output, stacks):
        ssh = FakeSSH({"docker ps": CommandResult(stdout=ps_output, exit_code=0)})
        ssh._docker_sudo = False
        ssh._apply_stack_status(stacks)
        return stacks

    def test_all_running(self):
        stacks = self._apply(
            "proj\t/opt/a\trunning\nproj\t/opt/a\trunning",
            [DockerStack("a", "/opt/a", "/opt/a/c.yml")],
        )
        self.assertEqual(stacks[0].status, "running")

    def test_mixed_is_partial(self):
        stacks = self._apply(
            "proj\t/opt/a\trunning\nproj\t/opt/a\texited",
            [DockerStack("a", "/opt/a", "/opt/a/c.yml")],
        )
        self.assertEqual(stacks[0].status, "partial")

    def test_renamed_project_matches_on_working_dir(self):
        stacks = self._apply(
            "totally-different\t/opt/a\trunning",
            [DockerStack("a", "/opt/a", "/opt/a/c.yml")],
        )
        self.assertEqual(stacks[0].status, "running")

    def test_trailing_slash_still_matches(self):
        stacks = self._apply(
            "proj\t/opt/a\trunning", [DockerStack("a", "/opt/a/", "/opt/a/c.yml")]
        )
        self.assertEqual(stacks[0].status, "running")

    def test_no_containers_is_stopped(self):
        stacks = self._apply("", [DockerStack("ghost", "/opt/g", "/opt/g/c.yml")])
        self.assertEqual(stacks[0].status, "stopped")

    def test_empty_stack_list_is_a_no_op(self):
        ssh = FakeSSH()
        ssh._apply_stack_status([])
        self.assertFalse(ssh.commands, "should not query Docker for nothing")


if __name__ == "__main__":
    unittest.main()
