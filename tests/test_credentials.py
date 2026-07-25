"""Tests for config persistence. The keyring is never touched here."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import credentials
from models.host import Host


class TestHostPersistence(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        self._patches = [
            mock.patch.object(credentials, "CONFIG_DIR", directory),
            mock.patch.object(credentials, "HOSTS_FILE", directory / "hosts.json"),
        ]
        for patch in self._patches:
            patch.start()
        self.path = directory / "hosts.json"

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._temp.cleanup()

    def test_round_trip(self):
        hosts = [Host("10.0.0.1", "root", label="A"), Host("10.0.0.2", "ev", port=2222)]
        credentials.save_hosts(hosts)

        loaded = [Host.from_dict(entry) for entry in credentials.load_hosts()]
        self.assertEqual([h.address for h in loaded], ["root@10.0.0.1", "ev@10.0.0.2:2222"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(credentials.load_hosts(), [])

    def test_corrupt_json_returns_empty_rather_than_raising(self):
        self.path.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(credentials.load_hosts(), [])

    def test_wrong_top_level_type_returns_empty(self):
        self.path.write_text('{"hosts": []}', encoding="utf-8")
        self.assertEqual(credentials.load_hosts(), [])

    def test_non_dict_entries_are_filtered(self):
        self.path.write_text('[{"hostname":"h","username":"u"}, "junk", 42]', encoding="utf-8")
        entries = credentials.load_hosts()
        self.assertEqual(len(entries), 1)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        credentials.save_hosts([Host("h", "u")])
        leftovers = list(Path(self._temp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temp file left behind: {leftovers}")

    def test_a_failed_write_preserves_the_previous_file(self):
        credentials.save_hosts([Host("original", "u")])
        original = self.path.read_text(encoding="utf-8")

        class Unserializable:
            def to_dict(self):
                raise ValueError("boom")

        with self.assertRaises(ValueError):
            credentials.save_hosts([Unserializable()])

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_saved_file_is_valid_json_with_no_secrets(self):
        credentials.save_hosts([Host("h", "u", key_path="/k")])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        serialized = json.dumps(data).lower()
        for forbidden in ("password", "passphrase", "secret"):
            self.assertNotIn(forbidden, serialized)

    def test_config_dir_is_created_on_demand(self):
        nested = Path(self._temp.name) / "deeper" / "still"
        with mock.patch.object(credentials, "CONFIG_DIR", nested):
            self.assertEqual(credentials.config_dir(), nested)
            self.assertTrue(nested.is_dir())


class TestAsyncSaving(unittest.TestCase):
    """Deferred writes: the UI thread must never wait on the disk."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        self._patches = [
            mock.patch.object(credentials, "CONFIG_DIR", directory),
            mock.patch.object(credentials, "HOSTS_FILE", directory / "hosts.json"),
        ]
        for patch in self._patches:
            patch.start()
        self.path = directory / "hosts.json"

    def tearDown(self):
        credentials.flush_pending_saves(5.0)
        for patch in self._patches:
            patch.stop()
        self._temp.cleanup()

    def test_async_save_lands_on_disk(self):
        credentials.save_hosts_async([Host("10.0.0.1", "root")])
        credentials.flush_pending_saves(5.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["hostname"], "10.0.0.1")

    def test_a_burst_coalesces_to_the_final_state(self):
        for index in range(40):
            credentials.save_hosts_async([Host(f"host{index}", "u")])
        credentials.flush_pending_saves(10.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["hostname"], "host39")

    def test_no_temp_file_is_left_behind(self):
        for index in range(10):
            credentials.save_hosts_async([Host(f"h{index}", "u")])
        credentials.flush_pending_saves(10.0)
        self.assertEqual(list(Path(self._temp.name).glob("*.tmp")), [])

    def test_serialization_happens_on_the_calling_thread(self):
        """The snapshot must be taken before returning, so later edits are safe."""
        host = Host("original", "u")
        credentials.save_hosts_async([host])
        host.hostname = "mutated-immediately-after"
        credentials.flush_pending_saves(5.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["hostname"], "original")

    def test_write_errors_reach_the_callback(self):
        errors: list[Exception] = []
        with mock.patch.object(
            credentials, "_write_payload", side_effect=OSError("disk full")
        ):
            credentials.save_hosts_async([Host("h", "u")], on_error=errors.append)
            credentials.flush_pending_saves(5.0)
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], OSError)

    def test_flush_with_nothing_pending_returns(self):
        credentials.flush_pending_saves(1.0)

    def test_serialize_hosts_is_valid_json(self):
        payload = credentials.serialize_hosts([Host("h", "u")])
        self.assertEqual(json.loads(payload)[0]["username"], "u")


class TestKeyringErrorHandling(unittest.TestCase):
    """A missing or broken keyring backend must not take the app down."""

    def test_get_password_survives_a_backend_error(self):
        import keyring.errors

        with mock.patch.object(
            credentials.keyring, "get_password",
            side_effect=keyring.errors.NoKeyringError("none"),
        ):
            self.assertEqual(credentials.get_password("h", "u"), "")

    def test_save_password_reports_failure_instead_of_raising(self):
        import keyring.errors

        with mock.patch.object(
            credentials.keyring, "set_password",
            side_effect=keyring.errors.NoKeyringError("none"),
        ):
            self.assertFalse(credentials.save_password("h", "u", "pw"))

    def test_delete_password_ignores_a_missing_entry(self):
        import keyring.errors

        with mock.patch.object(
            credentials.keyring, "delete_password",
            side_effect=keyring.errors.PasswordDeleteError("absent"),
        ):
            credentials.delete_password("h", "u")  # Must not raise.

    def test_get_password_normalises_none_to_empty(self):
        with mock.patch.object(credentials.keyring, "get_password", return_value=None):
            self.assertEqual(credentials.get_password("h", "u"), "")


if __name__ == "__main__":
    unittest.main()
