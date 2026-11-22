"""Tests for the log panel's queue handling.

No Tk root is created: both methods under test touch nothing but the queue, so
they are bound to a stand-in holding one. The distinction they encode matters —
a flush is deliberately bounded, and a clear must not be.
"""

import queue
import unittest

from gui.log_panel import FILTERS, LEVEL_COLORS, LogPanel


class _Panel:
    """Just enough of a LogPanel to exercise its queue methods."""

    _MAX_PER_FLUSH = LogPanel._MAX_PER_FLUSH

    drain = LogPanel._drain
    discard_queued = LogPanel._discard_queued

    def __init__(self, count: int):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        for index in range(count):
            self._queue.put((f"line {index}\n", "info"))

    @property
    def remaining(self) -> int:
        return self._queue.qsize()


class TestFlushIsBounded(unittest.TestCase):
    def test_one_flush_takes_at_most_its_limit(self):
        panel = _Panel(LogPanel._MAX_PER_FLUSH * 3)
        self.assertEqual(len(panel.drain()), LogPanel._MAX_PER_FLUSH)

    def test_drain_returns_oldest_first(self):
        panel = _Panel(3)
        self.assertEqual([text for text, _ in panel.drain()],
                         ["line 0\n", "line 1\n", "line 2\n"])

    def test_drain_of_an_empty_queue_is_empty(self):
        self.assertEqual(_Panel(0).drain(), [])


class TestClearIsUnbounded(unittest.TestCase):
    """Clear used to drain one flush's worth, so a backlog scrolled back in."""

    def test_a_backlog_larger_than_one_flush_is_fully_discarded(self):
        panel = _Panel(LogPanel._MAX_PER_FLUSH * 4 + 7)
        discarded = panel.discard_queued()
        self.assertEqual(discarded, LogPanel._MAX_PER_FLUSH * 4 + 7)
        self.assertEqual(panel.remaining, 0)

    def test_nothing_queued_discards_nothing(self):
        panel = _Panel(0)
        self.assertEqual(panel.discard_queued(), 0)

    def test_the_queue_is_reusable_afterwards(self):
        panel = _Panel(5)
        panel.discard_queued()
        panel._queue.put(("after\n", "warn"))
        self.assertEqual(panel.drain(), [("after\n", "warn")])


class TestSourcedLines(unittest.TestCase):
    """Lines that name the host they came from, for bulk runs."""

    @staticmethod
    def _logged(*args, **kwargs):
        panel = _Panel(0)
        LogPanel.log(panel, *args, **kwargs)
        return panel.drain()

    def test_source_and_colour_travel_with_the_line(self):
        ((text, level, mark, color),) = self._logged(
            "up to date", "success", source="docker-01", color="#6FA8FF"
        )
        self.assertTrue(text.endswith("up to date\n"))
        self.assertNotIn("docker-01", text)  # Inserted at display time, in colour.
        self.assertEqual((level, color), ("success", "#6FA8FF"))
        self.assertTrue(mark.startswith("docker-01"))

    def test_every_line_of_a_multi_line_message_is_attributed(self):
        items = self._logged("one\ntwo", source="pihole", color="#E58CFF")
        self.assertEqual([item[2].strip() for item in items], ["pihole", "pihole"])

    def test_plain_lines_carry_no_source(self):
        ((_text, _level, mark, _color),) = self._logged("Loaded 3 host(s).")
        self.assertEqual(mark, "")


class TestFilters(unittest.TestCase):
    def test_all_hides_nothing(self):
        self.assertEqual(FILTERS["All"], frozenset())

    def test_each_filter_is_stricter_than_the_last(self):
        self.assertLess(FILTERS["All"], FILTERS["Issues"])
        self.assertLess(FILTERS["Issues"], FILTERS["Errors"])

    def test_errors_always_stay_visible(self):
        for name, hidden in FILTERS.items():
            with self.subTest(name):
                self.assertNotIn("error", hidden)

    def test_filters_only_name_real_levels(self):
        for hidden in FILTERS.values():
            self.assertLessEqual(hidden, set(LEVEL_COLORS))


if __name__ == "__main__":
    unittest.main()
