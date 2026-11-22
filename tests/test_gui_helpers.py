"""Tests for the pure helpers and lookup tables behind the GUI windows.

No Tk root is created here — see :mod:`tests.test_code_editor`. Widget
construction is left alone deliberately; what is worth pinning down is the small
amount of real logic these modules hold, and the places where a table in ``gui/``
has to agree with a table in ``core/``. Those cross-table mismatches are the
failures that reach a user: a lifecycle button naming an action the client cannot
run fails at click time, and a prune target missing from the dialog's order is
simply absent from the window with nothing to show it was dropped.
"""

import unittest

from core.ssh_client import COMPOSE_ACTIONS, PRUNE_TARGETS
from core.ssh_config import (
    ACTION_ADD,
    ACTION_ATTACH_KEY,
    ACTION_SKIP,
    ImportCandidate,
    SSHConfigHost,
)
import struct
import zlib

from gui import maintenance, stack_window, theme
from gui.app import _host_colors, _source_label
from gui.host_list import _update_summary, os_badge_color
from gui.import_dialog import summarize
from models.host import UPDATES_FAILED, UPDATES_UNKNOWN, Host


def _host(**kwargs) -> Host:
    return Host("10.0.0.1", "root", **kwargs)


def _candidate(action: str) -> ImportCandidate:
    return ImportCandidate(SSHConfigHost("web", "10.0.0.1", "deploy"), action, "")


class TestUpdateSummary(unittest.TestCase):
    """The sidebar's per-host update line."""

    def test_unknown_shows_nothing(self):
        # An empty string is what makes _set_row hide the row entirely; a host
        # that has never been checked should not claim to be up to date.
        text, _color = _update_summary(_host(pending_updates=UPDATES_UNKNOWN))
        self.assertEqual(text, "")

    def test_failed_check_is_reported_not_hidden(self):
        text, color = _update_summary(_host(pending_updates=UPDATES_FAILED))
        self.assertIn("failed", text)
        self.assertNotEqual(color, "gray60")

    def test_zero_is_up_to_date(self):
        text, _color = _update_summary(_host(pending_updates=0))
        self.assertIn("up to date", text)

    def test_one_update_is_singular(self):
        text, _color = _update_summary(_host(pending_updates=1))
        self.assertIn("1 update available", text)

    def test_several_updates_are_plural(self):
        text, _color = _update_summary(_host(pending_updates=7))
        self.assertIn("7 updates available", text)

    def test_every_state_has_a_colour(self):
        for count in (UPDATES_UNKNOWN, UPDATES_FAILED, 0, 1, 42):
            with self.subTest(count=count):
                _text, color = _update_summary(_host(pending_updates=count))
                self.assertTrue(color)

    def test_the_four_states_are_visually_distinct(self):
        colors = {
            _update_summary(_host(pending_updates=count))[1]
            for count in (UPDATES_UNKNOWN, UPDATES_FAILED, 0, 3)
        }
        self.assertEqual(len(colors), 4)


class TestOsBadgeColor(unittest.TestCase):
    def test_no_host_falls_back_to_the_default(self):
        self.assertTrue(os_badge_color(None))

    def test_a_known_distro_gets_its_own_colour(self):
        debian = os_badge_color(_host(os_info="Debian GNU/Linux 12"))
        self.assertTrue(debian.startswith("#"))

    def test_an_unrecognised_distro_still_returns_a_colour(self):
        self.assertTrue(os_badge_color(_host(os_info="Nonexistent Linux 1.0")))


class TestImportSummary(unittest.TestCase):
    def test_nothing_applied(self):
        self.assertEqual(summarize([]), "nothing to import")

    def test_counts_are_reported(self):
        result = summarize(
            [_candidate(ACTION_ADD), _candidate(ACTION_ADD), _candidate(ACTION_ATTACH_KEY)]
        )
        self.assertIn("2 host(s) added", result)
        self.assertIn("1 key path(s) attached", result)

    def test_only_the_kinds_that_happened_are_mentioned(self):
        added_only = summarize([_candidate(ACTION_ADD)])
        self.assertIn("1 host(s) added", added_only)
        self.assertNotIn("key path", added_only)

        keyed_only = summarize([_candidate(ACTION_ATTACH_KEY)])
        self.assertIn("1 key path(s) attached", keyed_only)
        self.assertNotIn("added", keyed_only)

    def test_skipped_candidates_do_not_count(self):
        # plan_import returns skips too; only what was applied should be counted.
        self.assertEqual(summarize([_candidate(ACTION_SKIP)]), "nothing to import")


class TestStackActionTables(unittest.TestCase):
    """``gui.stack_window`` buttons against the actions ``core`` can run."""

    def test_every_button_maps_to_a_real_compose_action(self):
        for label, action, *_rest in stack_window._ACTIONS:
            with self.subTest(label):
                self.assertIn(action, COMPOSE_ACTIONS)

    def test_action_names_are_unique(self):
        actions = [action for _label, action, *_rest in stack_window._ACTIONS]
        self.assertEqual(len(actions), len(set(actions)))

    def test_every_confirmed_action_has_a_confirmation_message(self):
        # Without one, _run_action would show an empty dialog.
        for label, action, confirm, *_rest in stack_window._ACTIONS:
            if confirm:
                with self.subTest(label):
                    self.assertIn(action, stack_window._CONFIRMATIONS)

    def test_no_orphan_confirmation_messages(self):
        confirmed = {
            action for _l, action, confirm, *_r in stack_window._ACTIONS if confirm
        }
        self.assertEqual(set(stack_window._CONFIRMATIONS) - confirmed, set())

    def test_confirmations_interpolate_the_stack_name(self):
        for action, template in stack_window._CONFIRMATIONS.items():
            with self.subTest(action):
                self.assertIn("{name}", template)
                self.assertIn("web", template.format(name="web"))

    def test_the_destructive_actions_are_the_confirmed_ones(self):
        # down/stop/recreate all interrupt a running stack; up/restart/pull do
        # not, and prompting for them would train the user to click through.
        confirmed = {
            action for _l, action, confirm, *_r in stack_window._ACTIONS if confirm
        }
        self.assertEqual(confirmed, {"stop", "down", "recreate"})

    def test_tail_choices_are_usable(self):
        # Each is passed to `docker compose logs --tail`, which takes a number
        # or the literal 'all'.
        for choice in stack_window.TAIL_CHOICES:
            with self.subTest(choice):
                self.assertTrue(choice == "all" or choice.isdigit())


class TestMaintenanceOrder(unittest.TestCase):
    """``gui.maintenance._ORDER`` against ``core.ssh_client.PRUNE_TARGETS``."""

    def test_every_prune_target_is_shown(self):
        # A target missing from _ORDER is silently absent from the dialog.
        self.assertEqual(set(maintenance._ORDER), set(PRUNE_TARGETS))

    def test_no_duplicates(self):
        self.assertEqual(len(maintenance._ORDER), len(set(maintenance._ORDER)))

    def test_the_data_destroying_target_is_listed_last(self):
        # The module docstring promises safest first, data-destroying last.
        destructive = [
            name for name in maintenance._ORDER if PRUNE_TARGETS[name][2]
        ]
        self.assertEqual(destructive, [maintenance._ORDER[-1]])

    def test_exactly_one_target_is_marked_destructive(self):
        # If a second one is added, the dialog's single separated row and its
        # confirm-by-name prompt both need revisiting.
        flagged = [name for name, entry in PRUNE_TARGETS.items() if entry[2]]
        self.assertEqual(flagged, ["unused-volumes"])

    def test_every_target_has_a_command_and_a_description(self):
        for name, (command, description, _destructive) in PRUNE_TARGETS.items():
            with self.subTest(name):
                self.assertTrue(command.startswith("docker "))
                self.assertTrue(description.strip())

    def test_prune_commands_do_not_prompt(self):
        # These run over a non-interactive channel; a command that asks for
        # confirmation would hang until the timeout.
        for name, (command, *_rest) in PRUNE_TARGETS.items():
            with self.subTest(name):
                self.assertRegex(command, r"\s-\w*f")


class TestDiskUsageParsing(unittest.TestCase):
    """``gui.maintenance.parse_disk_usage`` turns ``docker system df`` into tiles."""

    SAMPLE = (
        "TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\n"
        "Images          14        9         6.21GB    2.03GB (32%)\n"
        "Containers      11        9         182MB     12MB (6%)\n"
        "Local Volumes   8         6         3.4GB     410MB (12%)\n"
        "Build Cache     41        0         1.1GB     1.1GB\n"
    )

    def test_every_row_is_read_in_order(self):
        rows = maintenance.parse_disk_usage(self.SAMPLE)
        self.assertEqual([r.kind for r in rows], ["Images", "Containers", "Volumes", "Build cache"])

    def test_columns_land_in_the_right_fields(self):
        images = maintenance.parse_disk_usage(self.SAMPLE)[0]
        self.assertEqual(
            (images.total, images.active, images.size, images.reclaimable),
            ("14", "9", "6.21GB", "2.03GB (32%)"),
        )

    def test_reclaimable_without_a_percentage(self):
        cache = maintenance.parse_disk_usage(self.SAMPLE)[3]
        self.assertEqual(cache.reclaimable, "1.1GB")

    def test_an_error_message_is_not_a_table(self):
        # An empty result is the signal to show the message itself instead.
        self.assertEqual(
            maintenance.parse_disk_usage("permission denied while trying to connect"), []
        )
        self.assertEqual(maintenance.parse_disk_usage(""), [])


class TestBulkIdentity(unittest.TestCase):
    """The colour and name column that tell hosts apart during a bulk run."""

    def test_neighbouring_hosts_get_different_colours(self):
        hosts = [_host(label=f"h{i}") for i in range(len(theme.HOST_COLORS))]
        colors = [_host_colors(hosts)[id(h)] for h in hosts]
        self.assertEqual(len(set(colors)), len(hosts))

    def test_colours_cycle_rather_than_run_out(self):
        self.assertEqual(theme.host_color(len(theme.HOST_COLORS)), theme.host_color(0))

    def test_a_host_keeps_its_colour_when_a_neighbour_sits_out(self):
        # Keyed by position in the whole list, not among the hosts taking part.
        hosts = [_host(label="a"), _host(label="b"), _host(label="c")]
        before = _host_colors(hosts)[id(hosts[2])]
        hosts[1].include_in_bulk = False
        self.assertEqual(_host_colors(hosts)[id(hosts[2])], before)

    def test_identity_colours_are_not_the_severity_colours(self):
        # A host must never look like an error, a warning or a success.
        severities = {theme.DANGER, theme.WARNING, theme.SUCCESS, theme.ORANGE}
        self.assertFalse(severities & set(theme.HOST_COLORS))

    def test_names_are_padded_to_one_column(self):
        self.assertEqual(_source_label("pihole", 10), "pihole    ")

    def test_long_names_are_cut_to_the_column(self):
        label = _source_label("nas-archive-with-a-long-label", 12)
        self.assertEqual(len(label), 12)
        self.assertTrue(label.endswith("…"))


class TestFitText(unittest.TestCase):
    """Ellipsising text to a pixel width. Measured here at 10 px a character."""

    @staticmethod
    def _measure(text: str) -> int:
        return 10 * len(text)

    def test_text_that_fits_is_untouched(self):
        self.assertEqual(theme.fit_text("docker-01", 90, self._measure), "docker-01")

    def test_long_text_is_cut_to_the_longest_prefix_that_fits(self):
        fitted = theme.fit_text("a-very-long-host-label", 100, self._measure)
        self.assertEqual(fitted, "a-very-lo…")
        self.assertLessEqual(self._measure(fitted), 100)

    def test_no_space_is_left_before_the_ellipsis(self):
        self.assertEqual(theme.fit_text("Debian GNU/Linux", 80, self._measure), "Debian…")

    def test_no_room_at_all_still_returns_something(self):
        self.assertEqual(theme.fit_text("docker-01", 5, self._measure), "…")

    def test_an_unknown_width_leaves_the_text_alone(self):
        # Before a widget is laid out its width is 0; cutting to that would blank it.
        self.assertEqual(theme.fit_text("docker-01", 0, self._measure), "docker-01")


class TestDrawnShapes(unittest.TestCase):
    """The pixel maths behind the host list's rounded corners, switch and dots."""

    @staticmethod
    def _alpha(pixels: bytes, width: int, x: int, y: int) -> int:
        return pixels[(y * width + x) * 4 + 3]

    def test_a_circle_is_solid_in_the_middle_and_clear_at_the_corners(self):
        pixels = theme.shape_pixels(12, 12, 6, "#3DD68C")
        self.assertEqual(len(pixels), 12 * 12 * 4)
        self.assertEqual(self._alpha(pixels, 12, 6, 6), 255)
        self.assertEqual(self._alpha(pixels, 12, 0, 0), 0)
        self.assertEqual(pixels[(6 * 12 + 6) * 4:(6 * 12 + 6) * 4 + 3], bytes((0x3D, 0xD6, 0x8C)))

    def test_edges_are_anti_aliased(self):
        pixels = theme.shape_pixels(12, 12, 6, "#FFFFFF")
        partial = {a for a in pixels[3::4] if 0 < a < 255}
        self.assertTrue(partial, "a circle with no partially covered pixels is aliased")

    def test_shapes_are_symmetric_for_odd_and_even_sizes(self):
        for width, height, radius in ((28, 15, 7.5), (9, 5, 2.5), (5, 5, 2.5), (3, 40, 1.5)):
            pixels = theme.shape_pixels(width, height, radius, "#FFFFFF")
            self.assertEqual(len(pixels), width * height * 4)
            for y in range(height):
                row = [self._alpha(pixels, width, x, y) for x in range(width)]
                self.assertEqual(row, row[::-1])
                self.assertEqual(
                    row, [self._alpha(pixels, width, x, height - 1 - y) for x in range(width)]
                )

    def test_an_outline_is_hollow(self):
        pixels = theme.shape_pixels(40, 20, 6, "#242933", outline=1)
        self.assertEqual(self._alpha(pixels, 40, 20, 10), 0)
        self.assertEqual(self._alpha(pixels, 40, 20, 0), 255)
        self.assertEqual(self._alpha(pixels, 40, 0, 10), 255)

    def test_alpha_scales_the_whole_shape(self):
        pixels = theme.shape_pixels(10, 10, 5, "#FFFFFF", alpha=0.2)
        self.assertEqual(max(pixels[3::4]), 51)

    def test_png_round_trips(self):
        rgba = bytes(range(24))  # 3 x 2 pixels
        data = theme._png(3, 2, rgba)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (3, 2))
        start = data.index(b"IDAT") + 4
        length = struct.unpack(">I", data[start - 8:start - 4])[0]
        raw = zlib.decompress(data[start:start + length])
        # Each scanline is a filter byte (0: none) followed by its pixels.
        self.assertEqual(raw, b"\x00" + rgba[:12] + b"\x00" + rgba[12:])


if __name__ == "__main__":
    unittest.main()
