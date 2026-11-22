"""Tests for the command palette's matching and the dialog message layout.

No Tk root is created: both are pure functions the widgets are built on.
"""

import unittest

from gui.dialogs import split_message
from gui.palette import Command, fuzzy_score, rank


def _commands(*titles: str) -> list[Command]:
    return [Command(title, lambda: None, order=i) for i, title in enumerate(titles)]


class TestFuzzyScore(unittest.TestCase):
    def test_empty_query_matches_everything(self):
        self.assertEqual(fuzzy_score("", "Scan stacks"), 0)

    def test_letters_must_appear_in_order(self):
        self.assertIsNotNone(fuzzy_score("scn", "Scan stacks"))
        self.assertIsNone(fuzzy_score("nsc", "Scan"))

    def test_every_word_must_match(self):
        self.assertIsNotNone(fuzzy_score("upd med", "Update stack media"))
        self.assertIsNone(fuzzy_score("upd zz", "Update stack media"))

    def test_case_is_ignored(self):
        self.assertEqual(fuzzy_score("SCAN", "scan stacks"), fuzzy_score("scan", "Scan Stacks"))

    def test_word_starts_beat_scattered_letters(self):
        self.assertGreater(fuzzy_score("med", "Manage media"),
                           fuzzy_score("med", "Manage metadata"))


class TestRank(unittest.TestCase):
    def test_no_query_keeps_the_given_order(self):
        commands = _commands("b", "a", "c")
        self.assertEqual([c.title for c in rank("", commands)], ["b", "a", "c"])

    def test_non_matches_are_dropped(self):
        self.assertEqual(rank("zzz", _commands("Scan stacks", "Cleanup")), [])

    def test_the_word_itself_ranks_first(self):
        # Greedy first-letter matching once put "Go to media-server" first,
        # matching M(anage)-e-d across two words.
        ranked = rank("med", _commands("Go to media-server", "Manage media"))
        self.assertEqual(ranked[0].title, "Manage media")

    def test_title_matches_beat_subtitle_matches(self):
        commands = [
            Command("Scan stacks", lambda: None, subtitle="media", order=0),
            Command("Manage media", lambda: None, order=1),
        ]
        self.assertEqual(rank("media", commands)[0].title, "Manage media")

    def test_keywords_are_searchable(self):
        commands = [Command("System update", lambda: None, keywords="apt upgrade")]
        self.assertEqual(len(rank("apt", commands)), 1)


class TestSplitMessage(unittest.TestCase):
    def test_short_messages_stay_whole(self):
        self.assertEqual(split_message("One line.\n\nAnother."), ("One line.\n\nAnother.", ""))

    def test_long_output_moves_below_the_first_paragraph(self):
        output = "\n".join(f"error {i}" for i in range(20))
        lead, detail = split_message(f"Docker rejected the file.\n\n{output}")
        self.assertEqual(lead, "Docker rejected the file.")
        self.assertEqual(detail, output)

    def test_long_text_without_paragraphs_splits_after_the_first_line(self):
        text = "\n".join(f"line {i}" for i in range(20))
        lead, detail = split_message(text)
        self.assertEqual(lead, "line 0")
        self.assertTrue(detail.startswith("line 1"))

    def test_empty(self):
        self.assertEqual(split_message(""), ("", ""))


if __name__ == "__main__":
    unittest.main()
