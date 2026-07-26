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
from gui import maintenance, stack_window
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


if __name__ == "__main__":
    unittest.main()
