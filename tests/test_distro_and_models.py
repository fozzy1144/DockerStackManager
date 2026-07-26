"""Tests for distro resolution, the data models, and the snippet library."""

import unittest

from core import compose, distro, snippets
from models.host import (
    UPDATES_FAILED,
    UPDATES_UNKNOWN,
    Container,
    DockerStack,
    Host,
    merge_stacks,
)


class TestDistroResolution(unittest.TestCase):
    def test_known_families(self):
        for os_id, expected in (
            ("ubuntu", "apt"),
            ("debian", "apt"),
            ("rocky", "dnf"),
            ("fedora", "dnf"),
            ("arch", "pacman"),
            ("alpine", "apk"),
            ("opensuse-leap", "zypper"),
        ):
            self.assertEqual(distro.resolve(os_id).name, expected, os_id)

    def test_id_like_fallback(self):
        # The reason derivatives work without an entry of their own.
        self.assertEqual(distro.resolve("myremix", "ubuntu debian").name, "apt")
        self.assertEqual(distro.resolve("unknown", "rhel fedora").name, "dnf")

    def test_quoted_id_like(self):
        self.assertEqual(distro.resolve("x", '"debian"').name, "apt")

    def test_case_insensitive(self):
        self.assertEqual(distro.resolve("Ubuntu").name, "apt")

    def test_unknown_falls_back_to_the_default(self):
        self.assertEqual(distro.resolve("plan9").name, distro.DEFAULT_MANAGER.name)

    def test_empty_input(self):
        self.assertEqual(distro.resolve("", "").name, distro.DEFAULT_MANAGER.name)

    def test_is_recognized(self):
        self.assertTrue(distro.is_recognized("debian"))
        self.assertTrue(distro.is_recognized("remix", "debian"))
        self.assertFalse(distro.is_recognized("plan9"))
        self.assertFalse(distro.is_recognized(""))

    def test_brand_colors(self):
        self.assertEqual(distro.brand_color("ubuntu"), "#E95420")
        self.assertEqual(distro.brand_color("remix", "ubuntu"), "#E95420")
        self.assertEqual(distro.brand_color("plan9"), distro.DEFAULT_BRAND_COLOR)

    def test_every_manager_has_both_commands(self):
        for manager in (distro.APT, distro.DNF, distro.PACMAN, distro.APK, distro.ZYPPER):
            self.assertTrue(manager.check_cmd, manager.name)
            self.assertTrue(manager.update_cmd, manager.name)

    def test_update_commands_are_non_interactive(self):
        """An unattended update must never stop at a prompt."""
        for manager in (distro.APT, distro.DNF, distro.PACMAN, distro.ZYPPER):
            command = manager.update_cmd
            self.assertTrue(
                any(
                    flag in command
                    for flag in ("-y", "--noconfirm", "--non-interactive")
                ),
                f"{manager.name} may prompt: {command}",
            )

    def test_os_info_defaults(self):
        info = distro.OSInfo()
        self.assertEqual((info.id, info.pretty, info.like), ("", "", ""))


class TestHost(unittest.TestCase):
    def test_display_name_prefers_the_label(self):
        self.assertEqual(Host("h", "u", label="Box").display_name, "Box")
        self.assertEqual(Host("h", "u").display_name, "h")

    def test_address_hides_the_default_port(self):
        self.assertEqual(Host("h", "u").address, "u@h")
        self.assertEqual(Host("h", "u", port=2222).address, "u@h:2222")

    def test_round_trip(self):
        host = Host(
            hostname="10.0.0.5",
            username="ev",
            port=2222,
            label="Media",
            os_info="debian",
            os_pretty="Debian 12",
            os_like="",
            stacks=[DockerStack("j", "/opt/j", "/opt/j/c.yml", "running", "j")],
        )
        host.pending_updates = 7

        data = host.to_dict()
        self.assertNotIn("pending_updates", data, "runtime state must not persist")

        restored = Host.from_dict(data)
        self.assertEqual(restored.address, "ev@10.0.0.5:2222")
        self.assertEqual(restored.stacks[0].status, "running")
        self.assertEqual(restored.stacks[0].project, "j")
        self.assertEqual(restored.pending_updates, UPDATES_UNKNOWN)

    def test_legacy_config_without_new_fields_still_loads(self):
        legacy = {
            "hostname": "h", "username": "u", "port": 22, "label": "",
            "key_path": "", "os_info": "ubuntu", "os_pretty": "Ubuntu",
            "stacks": [{"name": "n", "path": "/p", "compose_file": "/p/c.yml",
                        "status": "running"}],
        }
        host = Host.from_dict(legacy)
        self.assertEqual(host.os_like, "")
        self.assertEqual(host.stacks[0].project, "")

    def test_unknown_keys_are_ignored(self):
        host = Host.from_dict(
            {"hostname": "h", "username": "u", "field_from_the_future": 1}
        )
        self.assertEqual(host.hostname, "h")

    def test_missing_required_field_raises(self):
        with self.assertRaises((KeyError, TypeError)):
            Host.from_dict({"username": "u"})

    def test_junk_stack_entries_are_dropped(self):
        host = Host.from_dict(
            {"hostname": "h", "username": "u",
             "stacks": [{"no_path": 1}, "not a dict", None]}
        )
        self.assertEqual(host.stacks, [])

    def test_one_unloadable_stack_does_not_cost_the_host(self):
        # A record with a path but no compose_file used to raise out of
        # from_dict, so the caller skipped the entire host.
        host = Host.from_dict(
            {
                "hostname": "h",
                "username": "u",
                "stacks": [
                    {"path": "/opt/broken"},  # no name, no compose_file
                    {"name": "ok", "path": "/opt/ok",
                     "compose_file": "/opt/ok/docker-compose.yml"},
                ],
            }
        )
        self.assertEqual(host.hostname, "h")
        self.assertEqual([s.name for s in host.stacks], ["ok"])

    def test_stacks_not_a_list_is_ignored(self):
        host = Host.from_dict({"hostname": "h", "username": "u", "stacks": "nonsense"})
        self.assertEqual(host.stacks, [])


class TestMergeStacks(unittest.TestCase):
    """A rescan must not throw away the rollback points only we know about."""

    @staticmethod
    def _stack(path: str, **kwargs) -> DockerStack:
        return DockerStack(
            name=path.rsplit("/", 1)[-1],
            path=path,
            compose_file=f"{path}/docker-compose.yml",
            **kwargs,
        )

    def test_snapshot_survives_a_rescan(self):
        previous = [
            self._stack(
                "/opt/app",
                image_snapshot={"app:latest": "sha256:" + "a" * 64},
                snapshot_taken="2026-07-25 10:00",
            )
        ]
        merged = merge_stacks(previous, [self._stack("/opt/app")])
        self.assertEqual(merged[0].image_snapshot, {"app:latest": "sha256:" + "a" * 64})
        self.assertEqual(merged[0].snapshot_taken, "2026-07-25 10:00")
        self.assertTrue(merged[0].can_roll_back)

    def test_a_fresh_snapshot_is_not_overwritten(self):
        previous = [self._stack("/opt/app", image_snapshot={"app:1": "sha256:old"})]
        discovered = [self._stack("/opt/app", image_snapshot={"app:1": "sha256:new"})]
        merged = merge_stacks(previous, discovered)
        self.assertEqual(merged[0].image_snapshot, {"app:1": "sha256:new"})

    def test_trailing_slashes_still_match(self):
        previous = [self._stack("/opt/app/", image_snapshot={"a": "sha256:x"})]
        merged = merge_stacks(previous, [self._stack("/opt/app")])
        self.assertEqual(merged[0].image_snapshot, {"a": "sha256:x"})

    def test_a_moved_stack_does_not_inherit(self):
        previous = [self._stack("/opt/app", image_snapshot={"a": "sha256:x"})]
        merged = merge_stacks(previous, [self._stack("/srv/app")])
        self.assertEqual(merged[0].image_snapshot, {})
        self.assertFalse(merged[0].can_roll_back)

    def test_a_vanished_stack_is_not_resurrected(self):
        previous = [self._stack("/opt/gone", image_snapshot={"a": "sha256:x"})]
        merged = merge_stacks(previous, [self._stack("/opt/here")])
        self.assertEqual([s.path for s in merged], ["/opt/here"])

    def test_the_carried_snapshot_is_a_copy(self):
        # Sharing the dict would make an update to one stack mutate the other.
        previous = [self._stack("/opt/app", image_snapshot={"a": "sha256:x"})]
        merged = merge_stacks(previous, [self._stack("/opt/app")])
        merged[0].image_snapshot["b"] = "sha256:y"
        self.assertEqual(previous[0].image_snapshot, {"a": "sha256:x"})

    def test_empty_previous_list_is_a_no_op(self):
        discovered = [self._stack("/opt/app")]
        self.assertEqual(merge_stacks([], discovered), discovered)

    def test_survives_a_round_trip_through_json(self):
        # The point of the merge is a rollback that outlives the session.
        stack = self._stack(
            "/opt/app", image_snapshot={"a": "sha256:x"}, snapshot_taken="then"
        )
        host = Host.from_dict(Host("h", "u", stacks=[stack]).to_dict())
        merged = merge_stacks(host.stacks, [self._stack("/opt/app")])
        self.assertEqual(merged[0].image_snapshot, {"a": "sha256:x"})
        self.assertEqual(merged[0].snapshot_taken, "then")


class TestContainer(unittest.TestCase):
    def test_status_label_folds_in_health(self):
        container = Container("n", "s", state="running", health="unhealthy")
        self.assertEqual(container.status_label, "running (unhealthy)")

    def test_status_label_shows_exit_code(self):
        self.assertEqual(
            Container("n", "s", state="exited", exit_code=137).status_label,
            "exited (137)",
        )

    def test_clean_exit_has_no_code(self):
        self.assertEqual(Container("n", "s", state="exited").status_label, "exited")

    def test_unknown_state(self):
        self.assertEqual(Container("n", "s").status_label, "unknown")

    def test_is_running(self):
        self.assertTrue(Container("n", "s", state="running").is_running)
        self.assertFalse(Container("n", "s", state="exited").is_running)


class TestUpdateSentinels(unittest.TestCase):
    def test_sentinels_are_distinct_and_negative(self):
        self.assertNotEqual(UPDATES_UNKNOWN, UPDATES_FAILED)
        self.assertLess(UPDATES_UNKNOWN, 0)
        self.assertLess(UPDATES_FAILED, 0)


class TestSnippetLibrary(unittest.TestCase):
    """The examples ship as documentation, so they must actually be valid."""

    def test_library_is_not_empty(self):
        self.assertGreater(len(snippets.SNIPPETS), 15)

    def test_titles_are_unique(self):
        titles = [s.title for s in snippets.SNIPPETS]
        self.assertEqual(len(titles), len(set(titles)))

    def test_every_snippet_is_complete(self):
        for snippet in snippets.SNIPPETS:
            self.assertTrue(snippet.title)
            self.assertTrue(snippet.summary, snippet.title)
            self.assertTrue(snippet.body.strip(), snippet.title)
            self.assertIn(snippet.kind, ("service", "fragment", "root"), snippet.title)
            self.assertIn(snippet.category, snippets.CATEGORIES, snippet.title)
            self.assertGreater(
                len(snippet.details), 120, f"{snippet.title} needs real guidance"
            )

    def test_no_tabs_in_any_body(self):
        for snippet in snippets.SNIPPETS:
            self.assertNotIn("\t", snippet.body, snippet.title)

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_every_body_parses_as_yaml_in_context(self):
        for snippet in snippets.SNIPPETS:
            document = _wrap(snippet)
            result = compose.parse(document)
            self.assertTrue(
                result.ok,
                f"{snippet.title} does not parse: "
                f"{result.error.message if result.error else '?'}\n{document}",
            )

    def test_by_category_covers_everything(self):
        grouped = snippets.by_category()
        total = sum(len(items) for items in grouped.values())
        self.assertEqual(total, len(snippets.SNIPPETS))
        for category in grouped:
            self.assertIn(category, snippets.CATEGORIES)

    def test_find_by_title(self):
        first = snippets.SNIPPETS[0]
        self.assertIs(snippets.find(first.title), first)
        self.assertIsNone(snippets.find("no such snippet"))

    def test_reindent(self):
        result = snippets.reindent("a:\n  b: 1\n", 2)
        self.assertEqual(result, "  a:\n    b: 1\n")

    def test_reindent_leaves_blank_lines_empty(self):
        result = snippets.reindent("a: 1\n\nb: 2\n", 4)
        self.assertIn("\n\n", result)
        self.assertNotIn("    \n", result)

    def test_reindent_to_zero_is_identity(self):
        self.assertEqual(snippets.reindent("a: 1\n", 0), "a: 1\n")


def _wrap(snippet: snippets.Snippet) -> str:
    """Place a snippet body where it belongs, so it can be parsed standalone."""
    if snippet.kind == "root":
        return snippet.body
    if snippet.kind == "service":
        return "services:\n" + snippets.reindent(snippet.body, 2)
    return (
        "services:\n  app:\n    image: example:1\n"
        + snippets.reindent(snippet.body, 4)
    )


if __name__ == "__main__":
    unittest.main()
