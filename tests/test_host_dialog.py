"""Tests for the host dialog's input handling.

Only the pure helpers are exercised — no Tk root is created. Both of them guard
an entry field the user types into by hand, which is where the two bugs they fix
came from.
"""

import os
import unittest
from pathlib import Path

from gui.host_dialog import MAX_PORT, MIN_PORT, expand_key_path, parse_port


class TestExpandKeyPath(unittest.TestCase):
    def test_tilde_is_expanded(self):
        # The field's placeholder suggests exactly this form, and taking it
        # literally made a perfectly good key "not found".
        expanded = expand_key_path("~/.ssh/id_rsa")
        self.assertNotIn("~", expanded)
        self.assertEqual(Path(expanded), Path.home() / ".ssh" / "id_rsa")

    def test_absolute_path_is_left_alone(self):
        self.assertEqual(expand_key_path("/keys/id_ed25519"), "/keys/id_ed25519")

    def test_environment_variables_are_expanded(self):
        os.environ["DSM_TEST_KEYDIR"] = "/tmp/keys"
        try:
            self.assertEqual(
                expand_key_path("$DSM_TEST_KEYDIR/id_rsa"), "/tmp/keys/id_rsa"
            )
        finally:
            del os.environ["DSM_TEST_KEYDIR"]

    def test_surrounding_whitespace_and_quotes_are_stripped(self):
        # A Windows path with spaces is usually pasted in with its quotes.
        self.assertEqual(expand_key_path('  "/keys/my key"  '), "/keys/my key")

    def test_empty_stays_empty(self):
        for value in ("", "   ", '""'):
            self.assertEqual(expand_key_path(value), "")


class TestParsePort(unittest.TestCase):
    def test_ordinary_port(self):
        self.assertEqual(parse_port("2222"), 2222)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(parse_port("  22 "), 22)

    def test_boundaries_are_accepted(self):
        self.assertEqual(parse_port(str(MIN_PORT)), MIN_PORT)
        self.assertEqual(parse_port(str(MAX_PORT)), MAX_PORT)

    def test_out_of_range_is_rejected(self):
        # These parse fine as integers, and used to be accepted — then failed at
        # connect time with a message about the host rather than the port.
        for value in ("0", "-1", "65536", "99999"):
            self.assertIsNone(parse_port(value), value)

    def test_non_numeric_is_rejected(self):
        for value in ("", "abc", "22a", "2.2", "0x16", None):
            self.assertIsNone(parse_port(value), value)


if __name__ == "__main__":
    unittest.main()
