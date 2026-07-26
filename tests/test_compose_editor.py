"""Tests for the compose editor's placement and presentation logic.

No Tk root is created here — see :mod:`tests.test_code_editor` for why that
matters. The two behaviours worth pinning down are pure decisions the window
makes about text it already has:

* where an inserted snippet lands, which is the difference between valid YAML
  and a block nested one level too deep;
* which line of a multi-line Docker error reaches the one-line status bar.
"""

import unittest

from core import compose, snippets
from gui.compose_editor import _LEVEL_ICONS, ComposeEditor, _first_line


class _FakeEditor:
    """Stands in for :class:`gui.code_editor.CodeEditor`."""

    def __init__(self, indent: int):
        self._indent = indent

    def current_indent(self) -> int:
        return self._indent


class _Window:
    """The only state ``_snippet_indent`` reads off the window."""

    def __init__(self, indent: int):
        self._editor = _FakeEditor(indent)


def _indent_for(kind: str, cursor_indent: int) -> int:
    snippet = snippets.Snippet(
        title="t", category="c", kind=kind, summary="s", body="a:\n", details="d"
    )
    return ComposeEditor._snippet_indent(_Window(cursor_indent), snippet)


class TestSnippetIndent(unittest.TestCase):
    def test_root_block_always_goes_to_column_zero(self):
        # A top-level 'volumes:' pasted at the cursor's depth would land inside
        # whichever service the cursor happens to be in.
        for cursor_indent in (0, 2, 4, 8):
            with self.subTest(cursor_indent=cursor_indent):
                self.assertEqual(_indent_for("root", cursor_indent), 0)

    def test_cursor_indentation_wins(self):
        self.assertEqual(_indent_for("service", 6), 6)
        self.assertEqual(_indent_for("fragment", 6), 6)

    def test_service_falls_back_to_one_level_in(self):
        # Column 0 means "no indentation to infer from" — use the conventional
        # depth for the kind instead of pasting a service at the file's root.
        self.assertEqual(_indent_for("service", 0), 2)

    def test_fragment_falls_back_to_two_levels_in(self):
        self.assertEqual(_indent_for("fragment", 0), 4)

    def test_an_unknown_kind_is_treated_as_a_fragment(self):
        self.assertEqual(_indent_for("something-new", 0), 4)

    def test_the_fallbacks_produce_parseable_documents(self):
        # The reason those numbers are 2 and 4 in the first place.
        for kind, preamble in (
            ("service", "services:\n"),
            ("fragment", "services:\n  demo:\n    image: x:1\n"),
        ):
            body = "restart: unless-stopped\n"
            text = preamble + snippets.reindent(body, _indent_for(kind, 0))
            with self.subTest(kind):
                self.assertIsNone(compose.parse(text).error)


class TestFirstLine(unittest.TestCase):
    """``docker compose config`` failures arrive as a wall of text."""

    def test_single_line(self):
        self.assertEqual(_first_line("boom"), "boom")

    def test_leading_blank_lines_are_skipped(self):
        self.assertEqual(_first_line("\n\n  real problem\n"), "real problem")

    def test_later_lines_are_dropped(self):
        self.assertEqual(_first_line("first\nsecond\nthird"), "first")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(_first_line("   spaced   \nmore"), "spaced")

    def test_all_blank_input_returns_empty(self):
        for text in ("", "   ", "\n\n", "\t\n \n"):
            with self.subTest(repr(text)):
                self.assertEqual(_first_line(text), "")


class TestLevelIcons(unittest.TestCase):
    def test_every_lint_level_has_an_icon(self):
        # A level with no icon silently renders as a bullet in the checks list.
        for level in (compose.ERROR, compose.WARNING, compose.INFO):
            with self.subTest(level):
                self.assertIn(level, _LEVEL_ICONS)

    def test_icons_are_distinct(self):
        self.assertEqual(len(set(_LEVEL_ICONS.values())), len(_LEVEL_ICONS))


if __name__ == "__main__":
    unittest.main()
