"""Tests for the editor's pure helpers and its change-notification contract.

No Tk root is created here. The two guard tests exist because both of the
conditions they check caused an unbreakable event loop during development.
"""

import unittest

from gui.code_editor import INDENT, _ProxiedText, _split_comment


class TestCommentSplitting(unittest.TestCase):
    def test_plain_comment(self):
        code, at = _split_comment("image: nginx  # pinned")
        self.assertEqual(code, "image: nginx  ")
        self.assertEqual(at, 14)

    def test_whole_line_comment(self):
        code, at = _split_comment("# just a note")
        self.assertEqual(code, "")
        self.assertEqual(at, 0)

    def test_no_comment(self):
        code, at = _split_comment("image: nginx:1.27")
        self.assertEqual(code, "image: nginx:1.27")
        self.assertIsNone(at)

    def test_hash_inside_double_quotes_is_not_a_comment(self):
        code, at = _split_comment('password: "abc#def"')
        self.assertIsNone(at)
        self.assertEqual(code, 'password: "abc#def"')

    def test_hash_inside_single_quotes_is_not_a_comment(self):
        _code, at = _split_comment("tag: 'v1#2'")
        self.assertIsNone(at)

    def test_comment_after_a_quoted_string(self):
        code, at = _split_comment('name: "web" # the front end')
        self.assertEqual(at, 12)
        self.assertEqual(code, 'name: "web" ')

    def test_hash_without_preceding_space_is_not_a_comment(self):
        # Common in image tags and URLs; treating it as a comment hid real text.
        _code, at = _split_comment("image: repo/app#branch")
        self.assertIsNone(at)

    def test_hash_at_start_of_indented_line(self):
        _code, at = _split_comment("    # indented note")
        self.assertEqual(at, 4)

    def test_empty_line(self):
        self.assertEqual(_split_comment(""), ("", None))


class TestChangeNotificationGuards(unittest.TestCase):
    """Regressions: both of these produced an endless <<Change>> storm."""

    def test_edit_modified_is_not_treated_as_a_mutation(self):
        # The status bar queries `edit modified` on every change. If that query
        # counts as a change, the widget notifies itself forever.
        self.assertNotIn(("edit", "modified"), _ProxiedText._EDIT_MUTATIONS)
        self.assertNotIn("edit", _ProxiedText._MUTATING)

    def test_edit_separator_is_not_a_mutation(self):
        self.assertNotIn(("edit", "separator"), _ProxiedText._EDIT_MUTATIONS)

    def test_undo_and_redo_do_notify(self):
        for subcommand in ("undo", "redo", "reset"):
            self.assertIn(("edit", subcommand), _ProxiedText._EDIT_MUTATIONS)

    def test_content_mutations_notify(self):
        for subcommand in ("insert", "delete", "replace"):
            self.assertIn(subcommand, _ProxiedText._MUTATING)


class TestIndentation(unittest.TestCase):
    def test_indent_is_spaces_only(self):
        # YAML forbids tabs for indentation, so the editor must never insert one.
        self.assertNotIn("\t", INDENT)
        self.assertTrue(INDENT.strip() == "")
        self.assertEqual(len(INDENT), 2)


if __name__ == "__main__":
    unittest.main()
