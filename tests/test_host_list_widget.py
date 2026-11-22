"""The sidebar host list, driven as a real widget.

Everything in the list — cards, switches, the scrollbar — is drawn on one canvas
and hit-tested by hand, so the only honest test is to build it and poke it.
These need a display; without one (a headless Linux box, say) they are skipped.
The window is parked off-screen and nothing here touches the network or the
user's configuration.
"""

import time
import tkinter as tk
import unittest
from types import SimpleNamespace

from gui import theme
from gui.host_list import HostList
from models.host import Host

_root = None


def setUpModule():
    global _root
    try:
        import customtkinter as ctk

        _root = ctk.CTk()
    except tk.TclError as exc:  # No display to draw on.
        raise unittest.SkipTest(f"no display: {exc}")
    _root.geometry("280x360-4000-4000")
    _root.update()


def tearDownModule():
    if _root is not None:
        _root.destroy()


def _settle(seconds: float = 0.3) -> None:
    """Let timers and animations run for ``seconds``."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        _root.update()
        time.sleep(0.005)


def _hosts(count: int) -> list[Host]:
    return [
        Host(hostname=f"192.0.2.{index + 1}", username="deploy", label=f"host-{index:02d}",
             os_info="debian", os_pretty="Debian GNU/Linux 12 (bookworm)")
        for index in range(count)
    ]


def _at(x: int, y: int, **extra) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y, x_root=x, y_root=y, **extra)


class HostListCase(unittest.TestCase):
    COUNT = 3

    def setUp(self):
        self.calls: list[tuple[str, Host]] = []

        def record(kind):
            return lambda host: self.calls.append((kind, host))

        self.list = HostList(
            _root,
            on_select=record("select"),
            on_edit=record("edit"),
            on_remove=record("remove"),
            on_toggle=record("toggle"),
            on_activate=record("activate"),
        )
        self.list.pack(fill="both", expand=True)
        self.hosts = _hosts(self.COUNT)
        self.list.set_hosts(self.hosts)
        _root.update()

    def tearDown(self):
        self.list.destroy()
        _root.update()

    def card(self, index: int):
        return self.list._cards[index]

    def middle(self, index: int, part: str = "body") -> tuple[int, int]:
        """A window point inside one part of a card."""
        card = self.card(index)
        if part == "body":
            x, y = self.list._px(60), (card.top + card.bottom) // 2
        else:
            x0, y0, x1, y1 = card.boxes[part]
            x, y = (x0 + x1) // 2, (y0 + y1) // 2
        return x, y - self.list._origin

    def click(self, index: int, part: str) -> None:
        point = _at(*self.middle(index, part))
        self.list._on_press(point)
        self.list._on_release(point)


class TestDrawing(HostListCase):
    def test_one_card_per_host_in_order(self):
        self.assertEqual([card.host for card in self.list._cards], self.hosts)
        tops = [card.top for card in self.list._cards]
        self.assertEqual(tops, sorted(tops))

    def test_an_empty_list_explains_itself(self):
        self.list.set_hosts([])
        self.assertEqual(self.list._cards, [])
        texts = [self.list.itemcget(item, "text") for item in self.list.find_all()
                 if self.list.type(item) == "text"]
        self.assertTrue(any(texts))

    def test_a_long_name_is_cut_with_an_ellipsis(self):
        self.hosts[0].label = "nas-archive-with-a-very-long-descriptive-label"
        self.list.refresh()
        shown = self.list.itemcget(self.card(0).name, "text")
        self.assertTrue(shown.endswith("…"))
        self.assertLess(len(shown), len(self.hosts[0].label))

    def test_redrawing_keeps_card_state(self):
        state = self.list._states[id(self.hosts[0])]
        self.list.refresh()
        self.assertIs(self.list._states[id(self.hosts[0])], state)

    def test_removed_hosts_lose_their_state(self):
        gone = self.hosts[0]
        self.list.set_hosts(self.hosts[1:])
        self.assertNotIn(id(gone), self.list._states)


class TestPointer(HostListCase):
    def test_parts_of_a_card_are_told_apart(self):
        for part in ("body", "switch", "edit", "remove"):
            with self.subTest(part):
                card, found = self.list._locate(*self.middle(1, part))
                self.assertIs(card.host, self.hosts[1])
                self.assertEqual(found, part)

    def test_below_the_last_card_is_nothing(self):
        self.assertEqual(self.list._locate(40, self.card(-1).bottom + 5), (None, ""))

    def test_pressing_a_card_selects_it(self):
        self.list._on_press(_at(*self.middle(2)))
        self.assertEqual(self.calls, [("select", self.hosts[2])])

    def test_double_clicking_a_card_activates_it(self):
        self.list._on_press(_at(*self.middle(2)), double=True)
        self.assertEqual(self.calls, [("activate", self.hosts[2])])

    def test_the_switch_toggles_fleet_membership(self):
        self.assertTrue(self.hosts[0].include_in_bulk)
        self.click(0, "switch")
        self.assertFalse(self.hosts[0].include_in_bulk)
        self.assertEqual(self.calls, [("toggle", self.hosts[0])])
        _settle(0.25)  # The knob slides across.
        self.assertEqual(self.list._states[id(self.hosts[0])].knob, 0.0)

    def test_edit_and_remove_report_their_host(self):
        self.click(1, "edit")
        self.click(1, "remove")
        self.assertEqual(self.calls, [("edit", self.hosts[1]), ("remove", self.hosts[1])])

    def test_dragging_off_a_control_cancels_it(self):
        self.list._on_press(_at(*self.middle(0, "remove")))
        self.list._on_release(_at(*self.middle(1)))
        self.assertEqual(self.calls, [])

    def test_hover_follows_the_pointer(self):
        self.list._on_motion(_at(*self.middle(1, "edit")))
        card, part = self.list._hover
        self.assertIs(card.host, self.hosts[1])
        self.assertEqual(part, "edit")
        _settle(0.2)
        self.list._on_leave(None)
        self.assertEqual(self.list._hover, (None, ""))
        _settle(0.2)


class TestSelection(HostListCase):
    def test_the_selected_card_takes_the_raised_fill(self):
        self.list.set_active(self.hosts[1])
        _settle(0.25)
        self.assertEqual(self.list._states[id(self.hosts[1])].fill.lower(),
                         theme.SURFACE_2.lower())
        self.assertEqual(self.list._states[id(self.hosts[0])].fill.lower(),
                         theme.SIDEBAR.lower())

    def test_connection_and_identity_marks_survive_a_redraw(self):
        self.list.set_connected(self.hosts[0])
        self.list.mark(self.hosts[1], theme.host_color(1), working=True)
        self.assertTrue(self.card(1).halo)
        self.assertFalse(self.card(0).halo)
        self.assertIsNotNone(self.list._pulse_job)
        _settle(0.2)  # The halo breathes without complaint.

        self.list.mark(self.hosts[1], theme.host_color(1), working=False)
        self.assertFalse(self.card(1).halo)
        self.list.clear_marks()
        self.assertEqual(self.list._marks, {})


class TestScrolling(HostListCase):
    COUNT = 30

    def test_content_taller_than_the_view_scrolls(self):
        self.assertGreater(self.list._max_offset(), 0)

    def test_the_wheel_glides_one_notch(self):
        self.list._on_wheel(_at(40, 40, delta=-120))
        self.assertLess(self.list._origin, self.list._px(60))  # Not there yet…
        _settle(0.6)
        self.assertEqual(self.list._origin, self.list._px(60))

    def test_scrolling_stops_at_both_ends(self):
        self.list._on_wheel(_at(40, 40, delta=120))
        _settle(0.1)
        self.assertEqual(self.list._origin, 0)
        for _ in range(60):
            self.list._on_wheel(_at(40, 40, delta=-120))
        _settle(1.0)
        self.assertEqual(self.list._origin, self.list._max_offset())

    def test_hit_testing_follows_the_scroll_position(self):
        self.list._scroll_to(self.list._max_offset(), animate=False)
        y = self.list.winfo_height() - self.list._px(20)
        card, part = self.list._locate(40, y)
        self.assertIs(card.host, self.hosts[-1])
        self.assertEqual(part, "body")

    def test_selecting_a_host_out_of_view_reveals_it(self):
        self.list.set_active(self.hosts[-1])
        _settle(1.0)
        card = self.card(-1)
        self.assertLessEqual(card.bottom - self.list._origin, self.list.winfo_height())
        self.assertGreater(self.list._origin, 0)

    def test_the_thumb_can_be_dragged(self):
        x = self.list.winfo_width() - 2
        self.assertEqual(self.list._locate(x, 10), (None, "thumb"))
        self.list._on_press(_at(x, 10))
        self.assertTrue(self.list._dragging)
        self.list._on_motion(_at(x, self.list.winfo_height() - 5))
        self.assertGreater(self.list._origin, 0)
        self.list._on_release(_at(x, self.list.winfo_height() - 5))
        self.assertFalse(self.list._dragging)
        _settle(0.2)


if __name__ == "__main__":
    unittest.main()
