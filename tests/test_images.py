"""Tests for image freshness checking and stack rollback.

The design rule these enforce: an unknown answer is always preferred to a guessed
one. A false "up to date" hides a security update; a false "update available"
teaches you to ignore the indicator.
"""

import json
import unittest

from core.ssh_client import CommandResult, SSHClient, _find_digest, _unique
from models.host import DockerStack, ImageStatus

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "c" * 64


class FakeSSH(SSHClient):
    """Replays canned command results, matching on substrings in order."""

    def __init__(self, responses=None):
        super().__init__("host", "root")
        self._responses = responses or {}
        self.commands: list[str] = []
        self._compose_cmd = "docker compose"
        self._docker_sudo = False

    def run(self, command, timeout=15, sudo=False, raw=False):
        self.commands.append(command)
        for needle, result in self._responses.items():
            if needle in command:
                return result
        return CommandResult(stderr="unmatched", exit_code=1)

    def ran(self, fragment):
        return any(fragment in command for command in self.commands)


def stack():
    return DockerStack("app", "/opt/app", "/opt/app/docker-compose.yml")


class TestStackImages(unittest.TestCase):
    def test_images_are_listed(self):
        ssh = FakeSSH({"config --images": CommandResult(
            stdout="nginx:1.27\npostgres:16\n", exit_code=0)})
        self.assertEqual(ssh.stack_images(stack()), ["nginx:1.27", "postgres:16"])

    def test_duplicates_are_collapsed(self):
        ssh = FakeSSH({"config --images": CommandResult(
            stdout="nginx:1.27\nnginx:1.27\n", exit_code=0)})
        self.assertEqual(ssh.stack_images(stack()), ["nginx:1.27"])

    def test_compose_warnings_are_ignored(self):
        ssh = FakeSSH({"config --images": CommandResult(
            stdout="WARN[0000] something\nnginx:1.27\n", exit_code=0)})
        self.assertEqual(ssh.stack_images(stack()), ["nginx:1.27"])

    def test_failure_yields_no_images(self):
        self.assertEqual(FakeSSH().stack_images(stack()), [])

    def test_path_is_quoted(self):
        ssh = FakeSSH({"config --images": CommandResult(stdout="a:1", exit_code=0)})
        ssh.stack_images(DockerStack("s", "/opt/my app", "/opt/my app/c.yml"))
        self.assertTrue(ssh.ran("cd '/opt/my app'"), ssh.commands)


class TestImageFreshness(unittest.TestCase):
    def _ssh(self, *, local, remote_buildx=None, remote_manifest=None):
        responses = {
            "config --images": CommandResult(stdout="nginx:1.27", exit_code=0),
            "json .RepoDigests": local,
        }
        if remote_buildx is not None:
            responses["buildx imagetools"] = remote_buildx
        if remote_manifest is not None:
            responses["manifest inspect"] = remote_manifest
        return FakeSSH(responses)

    def test_matching_digests_are_up_to_date(self):
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stdout=DIGEST_A, exit_code=0),
        )
        status = ssh.check_stack_images(stack())[0]
        self.assertFalse(status.update_available)
        self.assertEqual(status.label, "up to date")

    def test_differing_digests_mean_an_update(self):
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stdout=DIGEST_B, exit_code=0),
        )
        status = ssh.check_stack_images(stack())[0]
        self.assertTrue(status.update_available)
        self.assertEqual(status.label, "update available")

    def test_any_matching_local_digest_counts(self):
        # An image can carry several repo digests; matching one is enough.
        ssh = self._ssh(
            local=CommandResult(
                stdout=json.dumps([f"other@{DIGEST_B}", f"nginx@{DIGEST_A}"]),
                exit_code=0,
            ),
            remote_buildx=CommandResult(stdout=DIGEST_A, exit_code=0),
        )
        self.assertFalse(ssh.check_stack_images(stack())[0].update_available)

    def test_unreachable_registry_is_unknown_not_stale(self):
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stderr="no such host", exit_code=1),
            remote_manifest=CommandResult(stderr="no such host", exit_code=1),
        )
        status = ssh.check_stack_images(stack())[0]
        self.assertIsNone(status.update_available)
        self.assertEqual(status.label, "unknown")
        self.assertIn("no such host", status.detail)

    def test_multi_arch_without_buildx_is_unknown(self):
        """The false-positive this guards against.

        A manifest list is stored locally under its index digest, which
        `docker manifest inspect` does not report — comparing against the
        per-platform digests would claim an update every time.
        """
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stderr="buildx not found", exit_code=1),
            remote_manifest=CommandResult(
                stdout=json.dumps({"manifests": [{"digest": DIGEST_B}]}), exit_code=0
            ),
        )
        status = ssh.check_stack_images(stack())[0]
        self.assertIsNone(status.update_available)
        self.assertIn("multi-architecture", status.detail)

    def test_single_arch_manifest_fallback_works(self):
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stderr="buildx not found", exit_code=1),
            remote_manifest=CommandResult(
                stdout=json.dumps({"config": {"digest": DIGEST_A}}), exit_code=0
            ),
        )
        self.assertFalse(ssh.check_stack_images(stack())[0].update_available)

    def test_image_absent_locally_is_unknown(self):
        ssh = self._ssh(
            local=CommandResult(stderr="No such image", exit_code=1),
            remote_buildx=CommandResult(stdout=DIGEST_A, exit_code=0),
        )
        status = ssh.check_stack_images(stack())[0]
        self.assertIsNone(status.update_available)
        self.assertIn("not present locally", status.detail)

    def test_unparsable_local_digests_are_unknown(self):
        ssh = self._ssh(
            local=CommandResult(stdout="not json", exit_code=0),
            remote_buildx=CommandResult(stdout=DIGEST_A, exit_code=0),
        )
        self.assertIsNone(ssh.check_stack_images(stack())[0].update_available)

    def test_no_images_means_no_statuses(self):
        self.assertEqual(FakeSSH().check_stack_images(stack()), [])

    def test_buildx_is_tried_before_manifest(self):
        ssh = self._ssh(
            local=CommandResult(stdout=json.dumps([f"nginx@{DIGEST_A}"]), exit_code=0),
            remote_buildx=CommandResult(stdout=DIGEST_A, exit_code=0),
            remote_manifest=CommandResult(stdout="{}", exit_code=0),
        )
        ssh.check_stack_images(stack())
        self.assertTrue(ssh.ran("buildx imagetools"))
        self.assertFalse(ssh.ran("manifest inspect"))


class TestSnapshot(unittest.TestCase):
    def test_image_ids_are_recorded(self):
        ssh = FakeSSH({
            "config --images": CommandResult(stdout="nginx:1.27", exit_code=0),
            "{{.Id}}": CommandResult(stdout=IMAGE_ID, exit_code=0),
        })
        self.assertEqual(ssh.snapshot_images(stack()), {"nginx:1.27": IMAGE_ID})

    def test_non_digest_output_is_rejected(self):
        ssh = FakeSSH({
            "config --images": CommandResult(stdout="nginx:1.27", exit_code=0),
            "{{.Id}}": CommandResult(stdout="garbage", exit_code=0),
        })
        self.assertEqual(ssh.snapshot_images(stack()), {})

    def test_missing_image_is_omitted(self):
        ssh = FakeSSH({
            "config --images": CommandResult(stdout="a:1\nb:2", exit_code=0),
        })
        self.assertEqual(ssh.snapshot_images(stack()), {})


class TestRollback(unittest.TestCase):
    def test_empty_snapshot_refuses(self):
        lines: list[str] = []
        self.assertFalse(FakeSSH().rollback_stack(stack(), {}, lines.append))
        self.assertTrue(any("No previous image versions" in line for line in lines))

    def test_missing_image_aborts_without_tagging(self):
        """A partial rollback would match neither version, so it must not start."""
        ssh = FakeSSH()  # every inspect fails
        lines: list[str] = []
        result = ssh.rollback_stack(stack(), {"nginx:1.27": IMAGE_ID}, lines.append)
        self.assertFalse(result)
        self.assertFalse(ssh.ran("docker tag"), "must not tag anything")
        self.assertTrue(any("no longer on the host" in line for line in lines))
        self.assertTrue(any("prune" in line for line in lines))

    def test_successful_rollback_tags_then_recreates(self):
        ssh = FakeSSH({
            "{{.Id}}": CommandResult(stdout=IMAGE_ID, exit_code=0),
            "docker tag": CommandResult(exit_code=0),
        })
        recreated: list[str] = []
        ssh.compose_action = lambda st, action, on_line: recreated.append(action) or True

        lines: list[str] = []
        result = ssh.rollback_stack(stack(), {"nginx:1.27": IMAGE_ID}, lines.append)

        self.assertTrue(result)
        self.assertTrue(ssh.ran("docker tag"))
        self.assertEqual(recreated, ["recreate"],
                         "must force recreation, or containers keep the new image")

    def test_failed_tag_stops_the_rollback(self):
        ssh = FakeSSH({
            "{{.Id}}": CommandResult(stdout=IMAGE_ID, exit_code=0),
            "docker tag": CommandResult(stderr="denied", exit_code=1),
        })
        called: list[str] = []
        ssh.compose_action = lambda st, action, on_line: called.append(action) or True
        lines: list[str] = []
        self.assertFalse(ssh.rollback_stack(stack(), {"a:1": IMAGE_ID}, lines.append))
        self.assertEqual(called, [], "must not recreate after a failed tag")


class TestUpdateRecordsRollbackPoint(unittest.TestCase):
    def test_snapshot_is_taken_before_the_pull(self):
        order: list[str] = []
        ssh = FakeSSH({
            "config --images": CommandResult(stdout="nginx:1.27", exit_code=0),
            "{{.Id}}": CommandResult(stdout=IMAGE_ID, exit_code=0),
        })

        def fake_action(st, action, on_line):
            order.append(action)
            return True

        ssh.compose_action = fake_action
        target = stack()
        self.assertTrue(ssh.update_stack(target, lambda _line: None))

        self.assertEqual(order, ["pull", "up"])
        self.assertEqual(target.image_snapshot, {"nginx:1.27": IMAGE_ID})
        self.assertTrue(target.snapshot_taken)
        self.assertTrue(target.can_roll_back)

    def test_failed_pull_keeps_the_snapshot_and_skips_up(self):
        ssh = FakeSSH({
            "config --images": CommandResult(stdout="nginx:1.27", exit_code=0),
            "{{.Id}}": CommandResult(stdout=IMAGE_ID, exit_code=0),
        })
        ssh.compose_action = lambda st, action, on_line: action != "pull"
        target = stack()
        lines: list[str] = []
        self.assertFalse(ssh.update_stack(target, lines.append))
        self.assertTrue(target.can_roll_back)
        self.assertTrue(any("left running" in line for line in lines))

    def test_no_pull_means_no_snapshot(self):
        ssh = FakeSSH()
        ssh.compose_action = lambda st, action, on_line: True
        target = stack()
        ssh.update_stack(target, lambda _line: None, pull=False)
        self.assertFalse(target.can_roll_back)


class TestHelpers(unittest.TestCase):
    def test_find_digest(self):
        self.assertEqual(_find_digest(f"junk {DIGEST_A} more"), DIGEST_A)
        self.assertEqual(_find_digest("no digest here"), "")
        self.assertEqual(_find_digest(""), "")

    def test_find_digest_rejects_short_hashes(self):
        self.assertEqual(_find_digest("sha256:abc123"), "")

    def test_unique_preserves_order(self):
        self.assertEqual(_unique(["b", "a", "b", "c"]), ["b", "a", "c"])

    def test_image_status_labels(self):
        self.assertEqual(ImageStatus("x").label, "unknown")
        self.assertEqual(ImageStatus("x", update_available=True).label, "update available")
        self.assertEqual(ImageStatus("x", update_available=False).label, "up to date")


if __name__ == "__main__":
    unittest.main()
