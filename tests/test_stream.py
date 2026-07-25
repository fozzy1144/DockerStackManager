"""Tests for RemoteProcess: line assembly, cancellation, and timeouts.

A fake channel replays a script of stdout/stderr/timeout events, which is enough
to exercise every path through the output pump without a network.
"""

import socket
import unittest

from core.ssh_client import RemoteProcess


class FakeChannel:
    """Stands in for a paramiko Channel driven by a scripted sequence."""

    def __init__(self, script, exit_code=0, never_exits=False):
        self.script = list(script)
        self.exit_code = exit_code
        self.never_exits = never_exits
        self.timeout = None
        self.closed = False
        self._stderr: list[bytes] = []
        self._eof = False

    def settimeout(self, value):
        self.timeout = value

    def recv(self, _size):
        if self.closed:
            raise OSError("Socket is closed")
        while self.script:
            kind, payload = self.script.pop(0)
            if kind == "out":
                return payload
            if kind == "err":
                self._stderr.append(payload)
                continue
            if kind == "timeout":
                raise socket.timeout()
        self._eof = True
        return b""

    def recv_stderr_ready(self):
        return bool(self._stderr)

    def recv_stderr(self, _size):
        return self._stderr.pop(0)

    def exit_status_ready(self):
        return self._eof and not self.never_exits

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True


def pump(script, exit_code=0, timeout=600, never_exits=False):
    channel = FakeChannel(script, exit_code, never_exits)
    process = RemoteProcess(channel)
    lines: list[str] = []
    code = process.pump(lines.append, timeout)
    return lines, code, channel, process


class TestLineAssembly(unittest.TestCase):
    def test_line_split_across_chunks_is_rejoined(self):
        lines, code, _, _ = pump(
            [("out", b"Get:1 http://deb.debi"), ("out", b"an.org ok\nsecond\n")]
        )
        self.assertEqual(lines, ["Get:1 http://deb.debian.org ok", "second"])
        self.assertEqual(code, 0)

    def test_trailing_line_without_newline_is_emitted(self):
        lines, _, _, _ = pump([("out", b"done\nno trailing newline")])
        self.assertEqual(lines, ["done", "no trailing newline"])

    def test_many_lines_in_one_chunk(self):
        lines, _, _, _ = pump([("out", b"a\nb\nc\n")])
        self.assertEqual(lines, ["a", "b", "c"])

    def test_blank_lines_are_dropped(self):
        lines, _, _, _ = pump([("out", b"a\n\n\n\nb\n")])
        self.assertEqual(lines, ["a", "b"])

    def test_carriage_returns_are_stripped(self):
        lines, _, _, _ = pump([("out", b"pulling\r\ndone\r\n")])
        self.assertEqual(lines, ["pulling", "done"])

    def test_invalid_utf8_does_not_raise(self):
        lines, _, _, _ = pump([("out", b"caf\xe9 broken\n")])
        self.assertEqual(len(lines), 1)

    def test_sudo_prompt_is_suppressed(self):
        lines, _, _, _ = pump([("out", b"[sudo] password for ev: \nreal output\n")])
        self.assertEqual(lines, ["real output"])

    def test_stderr_is_interleaved(self):
        lines, _, _, _ = pump(
            [("out", b"one\n"), ("err", b"warning\n"), ("out", b"two\n")]
        )
        self.assertEqual(sorted(lines), ["one", "two", "warning"])

    def test_no_output_at_all(self):
        lines, code, _, _ = pump([])
        self.assertEqual(lines, [])
        self.assertEqual(code, 0)

    def test_socket_timeouts_are_not_end_of_stream(self):
        lines, code, _, _ = pump(
            [("out", b"start\n"), ("timeout", None), ("timeout", None), ("out", b"end\n")]
        )
        self.assertEqual(lines, ["start", "end"])
        self.assertEqual(code, 0)

    def test_channel_gets_a_read_timeout(self):
        _, _, channel, _ = pump([("out", b"x\n")])
        self.assertIsNotNone(channel.timeout)
        self.assertGreater(channel.timeout, 0)


class TestExitStatus(unittest.TestCase):
    def test_nonzero_exit_is_returned(self):
        _, code, _, _ = pump([("out", b"boom\n")], exit_code=100)
        self.assertEqual(code, 100)

    def test_timeout_aborts_and_closes(self):
        lines, code, channel, _ = pump(
            [("timeout", None)] * 5, timeout=0, never_exits=True
        )
        self.assertEqual(code, -1)
        self.assertTrue(channel.closed)
        self.assertTrue(any("Timed out" in line for line in lines))


class TestCancellation(unittest.TestCase):
    """Stop() is what the log viewer's button relies on."""

    def test_stop_before_pump_returns_cleanly(self):
        channel = FakeChannel([("out", b"a\n")], never_exits=True)
        process = RemoteProcess(channel)
        process.stop()

        lines: list[str] = []
        code = process.pump(lines.append, 600)

        self.assertEqual(code, 0, "a cancelled command is not a failure")
        self.assertTrue(channel.closed)
        self.assertTrue(process.stopped)

    def test_stop_is_idempotent(self):
        channel = FakeChannel([])
        process = RemoteProcess(channel)
        process.stop()
        process.stop()
        self.assertTrue(channel.closed)

    def test_read_error_after_stop_is_not_reported_as_a_failure(self):
        # stop() closes the channel, which surfaces as an OSError mid-read.
        channel = FakeChannel([("out", b"a\n")], never_exits=True)
        process = RemoteProcess(channel)
        lines: list[str] = []

        original_recv = channel.recv
        calls = {"n": 0}

        def recv_then_die(size):
            calls["n"] += 1
            if calls["n"] > 1:
                process.stop()
                raise OSError("Socket is closed")
            return original_recv(size)

        channel.recv = recv_then_die
        code = process.pump(lines.append, 600)

        self.assertEqual(code, 0)
        self.assertNotIn(
            "Connection lost", " ".join(lines), "a deliberate stop is not an error"
        )

    def test_genuine_connection_loss_is_reported(self):
        channel = FakeChannel([("out", b"a\n")], never_exits=True)
        process = RemoteProcess(channel)
        lines: list[str] = []

        def recv_boom(_size):
            raise EOFError("peer went away")

        channel.recv = recv_boom
        code = process.pump(lines.append, 600)

        self.assertEqual(code, -1)
        self.assertTrue(any("Connection lost" in line for line in lines))

    def test_stopped_flag_starts_false(self):
        self.assertFalse(RemoteProcess(FakeChannel([])).stopped)


if __name__ == "__main__":
    unittest.main()
