"""Tests for the OpenSSH config parser and the import planner."""

import tempfile
import unittest
from pathlib import Path

from core import ssh_config
from core.ssh_config import (
    ACTION_ADD,
    ACTION_ATTACH_KEY,
    ACTION_SKIP,
    SSHConfigHost,
    parse_text,
    plan_import,
)
from models.host import Host


def by_alias(hosts) -> dict[str, SSHConfigHost]:
    return {host.alias: host for host in hosts}


class TestBasicParsing(unittest.TestCase):
    def test_single_host(self):
        hosts = parse_text(
            "Host web\n  HostName 10.0.0.1\n  User deploy\n  Port 2222\n"
        )
        self.assertEqual(len(hosts), 1)
        host = hosts[0]
        self.assertEqual(host.alias, "web")
        self.assertEqual(host.hostname, "10.0.0.1")
        self.assertEqual(host.user, "deploy")
        self.assertEqual(host.port, 2222)
        self.assertEqual(host.address, "deploy@10.0.0.1:2222")

    def test_hostname_defaults_to_the_alias(self):
        hosts = parse_text("Host example.com\n  User root\n")
        self.assertEqual(hosts[0].hostname, "example.com")

    def test_port_defaults_to_22(self):
        self.assertEqual(parse_text("Host a\n  User u\n")[0].port, 22)

    def test_address_omits_the_default_port(self):
        self.assertEqual(parse_text("Host a\n  User u\n")[0].address, "u@a")

    def test_keywords_are_case_insensitive(self):
        hosts = parse_text("HOST web\n  hostname 10.0.0.1\n  USER deploy\n")
        self.assertEqual(hosts[0].hostname, "10.0.0.1")
        self.assertEqual(hosts[0].user, "deploy")

    def test_equals_separator(self):
        hosts = parse_text("Host=web\n  HostName=10.0.0.1\n  User=deploy\n")
        self.assertEqual(hosts[0].hostname, "10.0.0.1")
        self.assertEqual(hosts[0].user, "deploy")

    def test_comments_and_blank_lines(self):
        hosts = parse_text(
            "# a comment\n\nHost web\n  # indented comment\n  User deploy\n\n"
        )
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].user, "deploy")

    def test_several_hosts(self):
        hosts = parse_text(
            "Host a\n  User ua\nHost b\n  User ub\nHost c\n  User uc\n"
        )
        self.assertEqual([h.alias for h in hosts], ["a", "b", "c"])
        self.assertEqual([h.user for h in hosts], ["ua", "ub", "uc"])

    def test_no_indentation_required(self):
        hosts = parse_text("Host a\nUser ua\nHost b\nUser ub\n")
        self.assertEqual(by_alias(hosts)["a"].user, "ua")
        self.assertEqual(by_alias(hosts)["b"].user, "ub")

    def test_empty_input(self):
        self.assertEqual(parse_text(""), [])

    def test_invalid_port_falls_back(self):
        for value in ("notanumber", "0", "70000", "-1"):
            hosts = parse_text(f"Host a\n  User u\n  Port {value}\n")
            self.assertEqual(hosts[0].port, 22, value)


class TestPatternsAndDefaults(unittest.TestCase):
    def test_wildcard_blocks_are_not_importable(self):
        hosts = parse_text("Host *\n  User default\nHost web\n  HostName 10.0.0.1\n")
        self.assertEqual([h.alias for h in hosts], ["web"])

    def test_wildcard_supplies_defaults(self):
        hosts = parse_text("Host *\n  User default\nHost web\n  HostName 10.0.0.1\n")
        self.assertEqual(hosts[0].user, "default")

    def test_first_obtained_value_wins(self):
        """OpenSSH never lets a later block override an earlier one."""
        hosts = parse_text(
            "Host web\n  User first\nHost web\n  User second\n"
        )
        self.assertEqual(hosts[0].user, "first")

    def test_earlier_wildcard_beats_later_specific_block(self):
        # Counter-intuitive but correct: this is why `Host *` belongs last.
        hosts = parse_text("Host *\n  User global\nHost web\n  User specific\n")
        self.assertEqual(hosts[0].user, "global")

    def test_later_wildcard_does_not_override(self):
        hosts = parse_text("Host web\n  User specific\nHost *\n  User global\n")
        self.assertEqual(hosts[0].user, "specific")

    def test_settings_before_any_host_line_are_global(self):
        hosts = parse_text("User early\nHost web\n  HostName 10.0.0.1\n")
        self.assertEqual(hosts[0].user, "early")

    def test_multiple_patterns_on_one_line(self):
        hosts = parse_text("Host alpha beta\n  User shared\n")
        self.assertEqual([h.alias for h in hosts], ["alpha", "beta"])
        self.assertTrue(all(h.user == "shared" for h in hosts))

    def test_glob_pattern_applies_to_matching_alias(self):
        hosts = parse_text(
            "Host prod-*\n  User produser\nHost prod-web\n  HostName 10.0.0.1\n"
        )
        self.assertEqual(hosts[0].alias, "prod-web")
        self.assertEqual(hosts[0].user, "produser")

    def test_negated_pattern_excludes(self):
        hosts = parse_text(
            "Host * !secret\n  User general\n"
            "Host normal\n  HostName 10.0.0.1\n"
            "Host secret\n  HostName 10.0.0.2\n"
        )
        aliases = by_alias(hosts)
        self.assertEqual(aliases["normal"].user, "general")
        self.assertEqual(aliases["secret"].user, "")

    def test_duplicate_alias_appears_once(self):
        hosts = parse_text("Host web\n  User a\nHost web\n  Port 2222\n")
        self.assertEqual(len(hosts), 1)
        # And both blocks contribute, first-wins per keyword.
        self.assertEqual(hosts[0].user, "a")
        self.assertEqual(hosts[0].port, 2222)

    def test_match_blocks_are_skipped(self):
        hosts = parse_text(
            "Host web\n  User real\n"
            "Match host web\n  User conditional\n"
            "Host other\n  User otheruser\n"
        )
        aliases = by_alias(hosts)
        self.assertEqual(aliases["web"].user, "real")
        self.assertEqual(aliases["other"].user, "otheruser")


class TestIdentityFiles(unittest.TestCase):
    def test_identity_file_is_kept(self):
        hosts = parse_text("Host a\n  User u\n  IdentityFile /keys/id_ed25519\n")
        self.assertEqual(hosts[0].identity_file, "/keys/id_ed25519")

    def test_tilde_is_expanded(self):
        hosts = parse_text("Host a\n  User u\n  IdentityFile ~/.ssh/id_rsa\n")
        self.assertNotIn("~", hosts[0].identity_file)
        self.assertTrue(hosts[0].identity_file.endswith("id_rsa"))

    def test_windows_path_survives(self):
        hosts = parse_text(
            "Host a\n  User u\n  IdentityFile C:\\Users\\example\\.ssh\\id_ed25519\n"
        )
        self.assertEqual(
            hosts[0].identity_file, "C:\\Users\\example\\.ssh\\id_ed25519"
        )

    def test_quoted_path_is_unquoted(self):
        hosts = parse_text('Host a\n  User u\n  IdentityFile "/keys/my key"\n')
        self.assertEqual(hosts[0].identity_file, "/keys/my key")

    def test_first_identity_file_wins(self):
        hosts = parse_text(
            "Host a\n  User u\n  IdentityFile /keys/one\n  IdentityFile /keys/two\n"
        )
        self.assertEqual(hosts[0].identity_file, "/keys/one")

    def test_proxy_jump_is_captured(self):
        hosts = parse_text("Host a\n  User u\n  ProxyJump bastion\n")
        self.assertEqual(hosts[0].proxy_jump, "bastion")

    def test_is_complete(self):
        self.assertTrue(parse_text("Host a\n  User u\n")[0].is_complete)
        self.assertFalse(parse_text("Host a\n")[0].is_complete)


class TestIncludes(unittest.TestCase):
    def test_include_is_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "extra").write_text(
                "Host included\n  HostName 10.0.0.9\n  User inc\n", encoding="utf-8"
            )
            (base / "config").write_text(
                "Include extra\nHost main\n  HostName 10.0.0.1\n  User main\n",
                encoding="utf-8",
            )
            hosts = by_alias(ssh_config.parse_file(base / "config"))
            self.assertIn("included", hosts)
            self.assertEqual(hosts["included"].hostname, "10.0.0.9")
            self.assertEqual(hosts["main"].user, "main")

    def test_include_glob(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "conf.d").mkdir()
            (base / "conf.d" / "a.conf").write_text(
                "Host from-a\n  User ua\n", encoding="utf-8"
            )
            (base / "conf.d" / "b.conf").write_text(
                "Host from-b\n  User ub\n", encoding="utf-8"
            )
            (base / "config").write_text("Include conf.d/*.conf\n", encoding="utf-8")
            hosts = by_alias(ssh_config.parse_file(base / "config"))
            self.assertEqual(set(hosts), {"from-a", "from-b"})

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(ssh_config.parse_file(Path("no-such-config")), [])

    def test_include_cycle_terminates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "config").write_text(
                "Include config\nHost a\n  User u\n", encoding="utf-8"
            )
            hosts = ssh_config.parse_file(base / "config")
            self.assertTrue(any(h.alias == "a" for h in hosts))


class TestVSCodeSettings(unittest.TestCase):
    def test_jsonc_with_comments_and_trailing_commas(self):
        parsed = ssh_config._loads_jsonc(
            '{\n  // a comment\n  "remote.SSH.configFile": "~/custom",\n'
            '  /* block */ "other": 1,\n}\n'
        )
        self.assertEqual(parsed["remote.SSH.configFile"], "~/custom")

    def test_url_in_a_string_is_not_a_comment(self):
        parsed = ssh_config._loads_jsonc('{"url": "https://example.com/x"}')
        self.assertEqual(parsed["url"], "https://example.com/x")

    def test_escaped_quote_in_a_string(self):
        parsed = ssh_config._loads_jsonc('{"a": "say \\"hi\\""}')
        self.assertEqual(parsed["a"], 'say "hi"')

    def test_garbage_returns_none(self):
        self.assertIsNone(ssh_config._loads_jsonc("not json at all {{{"))

    def test_candidate_paths_are_deduplicated(self):
        paths = ssh_config.candidate_paths()
        lowered = [str(p).lower() for p in paths]
        self.assertEqual(len(lowered), len(set(lowered)))


class TestImportPlanning(unittest.TestCase):
    def test_new_host_is_added(self):
        config = [SSHConfigHost("web", "10.0.0.1", "deploy")]
        plan = plan_import(config, [])
        self.assertEqual(plan[0].action, ACTION_ADD)
        self.assertTrue(plan[0].actionable)

    def test_existing_host_is_skipped(self):
        config = [SSHConfigHost("web", "10.0.0.1", "deploy")]
        existing = [Host("10.0.0.1", "deploy", key_path="/keys/id")]
        plan = plan_import(config, existing)
        self.assertEqual(plan[0].action, ACTION_SKIP)
        self.assertFalse(plan[0].actionable)

    def test_existing_host_without_a_key_gets_one(self):
        config = [SSHConfigHost("web", "10.0.0.1", "deploy", identity_file="/keys/id")]
        existing = [Host("10.0.0.1", "deploy")]
        plan = plan_import(config, existing)
        self.assertEqual(plan[0].action, ACTION_ATTACH_KEY)
        self.assertEqual(plan[0].existing_index, 0)

    def test_no_key_in_config_means_nothing_to_attach(self):
        config = [SSHConfigHost("web", "10.0.0.1", "deploy")]
        existing = [Host("10.0.0.1", "deploy")]
        self.assertEqual(plan_import(config, existing)[0].action, ACTION_SKIP)

    def test_host_without_a_user_cannot_be_imported(self):
        plan = plan_import([SSHConfigHost("web", "10.0.0.1")], [])
        self.assertEqual(plan[0].action, ACTION_SKIP)
        self.assertIn("no User", plan[0].reason)

    def test_same_machine_different_account_is_a_new_host(self):
        config = [SSHConfigHost("web", "10.0.0.1", "other")]
        existing = [Host("10.0.0.1", "deploy")]
        self.assertEqual(plan_import(config, existing)[0].action, ACTION_ADD)

    def test_same_machine_different_port_is_a_new_host(self):
        config = [SSHConfigHost("web", "10.0.0.1", "deploy", port=2222)]
        existing = [Host("10.0.0.1", "deploy", port=22)]
        self.assertEqual(plan_import(config, existing)[0].action, ACTION_ADD)

    def test_matching_ignores_case(self):
        config = [SSHConfigHost("web", "Example.COM", "Deploy")]
        existing = [Host("example.com", "deploy", key_path="/k")]
        self.assertEqual(plan_import(config, existing)[0].action, ACTION_SKIP)

    def test_existing_index_points_at_the_right_host(self):
        config = [SSHConfigHost("c", "10.0.0.3", "u", identity_file="/k")]
        existing = [Host("10.0.0.1", "u"), Host("10.0.0.2", "u"), Host("10.0.0.3", "u")]
        plan = plan_import(config, existing)
        self.assertEqual(plan[0].existing_index, 2)

    def test_empty_inputs(self):
        self.assertEqual(plan_import([], []), [])
        self.assertEqual(plan_import([], [Host("h", "u")]), [])


if __name__ == "__main__":
    unittest.main()
