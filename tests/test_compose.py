"""Tests for compose parsing, linting, and diffing."""

import unittest
from unittest import mock

from core import compose


#: PyYAML is optional at runtime, so the structural rules are optional too.
needs_yaml = unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")


def _messages(findings):
    return " | ".join(f.message for f in findings)


def _levels(findings, level):
    return [f for f in findings if f.level == level]


VALID = """\
services:
  web:
    image: nginx:1.27-alpine
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - web-data:/data
    networks: [frontend]
    healthcheck:
      test: ["CMD", "true"]

volumes:
  web-data:

networks:
  frontend:
"""


@needs_yaml
class TestParse(unittest.TestCase):
    def test_valid_document(self):
        result = compose.parse(VALID)
        self.assertTrue(result.ok)
        self.assertIn("web", result.data["services"])

    def test_syntax_error_reports_line(self):
        result = compose.parse("services:\n  web:\n   image: a\n    bad: b\n")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertGreater(result.error.line, 0)
        self.assertIn("YAML syntax error", result.error.message)

    def test_empty_file(self):
        result = compose.parse("")
        self.assertFalse(result.ok)
        self.assertIn("empty", result.error.message.lower())

    def test_top_level_must_be_mapping(self):
        result = compose.parse("- one\n- two\n")
        self.assertFalse(result.ok)
        self.assertIn("mapping", result.error.message)


@needs_yaml
class TestLintCleanFile(unittest.TestCase):
    def test_no_errors(self):
        findings = compose.lint(VALID)
        self.assertEqual(_levels(findings, compose.ERROR), [], _messages(findings))


@needs_yaml
class TestLintRules(unittest.TestCase):
    def test_missing_services(self):
        findings = compose.lint("volumes:\n  data:\n")
        self.assertTrue(any("No services" in f.message for f in findings))

    def test_service_without_image_or_build(self):
        findings = compose.lint("services:\n  web:\n    restart: always\n")
        self.assertTrue(
            any("neither 'image' nor 'build'" in f.message for f in findings),
            _messages(findings),
        )

    def test_build_instead_of_image_is_accepted(self):
        findings = compose.lint("services:\n  web:\n    build: .\n    restart: always\n")
        self.assertFalse(any("neither" in f.message for f in findings))

    def test_latest_tag_warns(self):
        findings = compose.lint("services:\n  web:\n    image: nginx:latest\n")
        self.assertTrue(any("latest" in f.message for f in findings))

    def test_missing_tag_warns(self):
        findings = compose.lint("services:\n  web:\n    image: nginx\n")
        self.assertTrue(any("latest" in f.message for f in findings))

    def test_registry_with_port_is_not_mistaken_for_a_tag(self):
        # registry:5000/app has a colon, but no tag — the warning should fire.
        findings = compose.lint("services:\n  w:\n    image: registry:5000/app\n")
        self.assertTrue(any("latest" in f.message for f in findings))

    def test_pinned_tag_does_not_warn(self):
        findings = compose.lint("services:\n  w:\n    image: nginx:1.27\n")
        self.assertFalse(any("latest" in f.message for f in findings))

    def test_digest_pin_does_not_warn(self):
        findings = compose.lint("services:\n  w:\n    image: nginx@sha256:abc123\n")
        self.assertFalse(any("latest" in f.message for f in findings))

    def test_missing_restart_warns(self):
        findings = compose.lint("services:\n  w:\n    image: nginx:1.27\n")
        self.assertTrue(any("restart policy" in f.message for f in findings))

    def test_deploy_counts_as_a_restart_policy(self):
        text = "services:\n  w:\n    image: nginx:1.27\n    deploy:\n      replicas: 1\n"
        self.assertFalse(any("restart policy" in f.message for f in compose.lint(text)))

    def test_privileged_warns(self):
        text = "services:\n  w:\n    image: a:1\n    restart: always\n    privileged: true\n"
        findings = compose.lint(text)
        self.assertTrue(any("privileged" in f.message for f in findings))

    def test_duplicate_container_name_is_an_error(self):
        text = (
            "services:\n"
            "  a:\n    image: x:1\n    container_name: dup\n    restart: always\n"
            "  b:\n    image: y:1\n    container_name: dup\n    restart: always\n"
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("dup" in f.message for f in errors), _messages(errors))

    def test_undeclared_named_volume_is_an_error(self):
        text = "services:\n  w:\n    image: x:1\n    volumes:\n      - data:/var/data\n"
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("data" in f.message for f in errors), _messages(errors))

    def test_declared_named_volume_is_fine(self):
        text = (
            "services:\n  w:\n    image: x:1\n    volumes:\n      - data:/var/data\n"
            "volumes:\n  data:\n"
        )
        self.assertFalse(
            any("named volume" in f.message for f in compose.lint(text))
        )

    def test_bind_mounts_are_not_treated_as_named_volumes(self):
        for mount in ("./cfg:/cfg", "/srv/x:/x", "~/y:/y", "${DATA}:/z"):
            text = f"services:\n  w:\n    image: x:1\n    volumes:\n      - {mount}\n"
            self.assertFalse(
                any("named volume" in f.message for f in compose.lint(text)),
                f"{mount} was treated as a named volume",
            )

    def test_undeclared_network_is_an_error(self):
        text = "services:\n  w:\n    image: x:1\n    networks: [missing]\n"
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("missing" in f.message for f in errors))

    def test_external_network_declaration_satisfies_the_reference(self):
        text = (
            "services:\n  w:\n    image: x:1\n    networks: [proxy]\n"
            "networks:\n  proxy:\n    external: true\n"
        )
        self.assertFalse(any("proxy" in f.message for f in compose.lint(text)))

    def test_depends_on_unknown_service(self):
        text = "services:\n  w:\n    image: x:1\n    depends_on: [ghost]\n"
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("ghost" in f.message for f in errors))

    def test_depends_on_mapping_form(self):
        text = (
            "services:\n"
            "  w:\n    image: x:1\n    depends_on:\n      db:\n"
            "        condition: service_healthy\n"
            "  db:\n    image: y:1\n"
        )
        self.assertFalse(any("depends on" in f.message for f in compose.lint(text)))

    def test_port_collision_is_an_error(self):
        text = (
            "services:\n"
            '  a:\n    image: x:1\n    ports:\n      - "8080:80"\n'
            '  b:\n    image: y:1\n    ports:\n      - "8080:81"\n'
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("8080" in f.message for f in errors), _messages(errors))

    def test_same_port_different_protocol_is_fine(self):
        text = (
            "services:\n"
            '  a:\n    image: x:1\n    ports:\n      - "53:53/udp"\n'
            '  b:\n    image: y:1\n    ports:\n      - "53:53/tcp"\n'
        )
        self.assertFalse(any("53" in f.message for f in _levels(compose.lint(text), compose.ERROR)))

    def test_ephemeral_ports_never_collide(self):
        text = (
            "services:\n"
            '  a:\n    image: x:1\n    ports:\n      - "3000"\n'
            '  b:\n    image: y:1\n    ports:\n      - "3000"\n'
        )
        self.assertFalse(
            any("collision" in f.message or "published by both" in f.message
                for f in compose.lint(text))
        )

    def test_ip_prefixed_port_is_understood(self):
        text = (
            "services:\n"
            '  a:\n    image: x:1\n    ports:\n      - "127.0.0.1:8080:80"\n'
            '  b:\n    image: y:1\n    ports:\n      - "8080:80"\n'
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("8080" in f.message for f in errors))

    def test_long_syntax_ports(self):
        text = (
            "services:\n"
            "  a:\n    image: x:1\n    ports:\n      - target: 80\n        published: 9999\n"
            "  b:\n    image: y:1\n    ports:\n      - target: 81\n        published: 9999\n"
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("9999" in f.message for f in errors), _messages(errors))

    def test_plaintext_secret_warns(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      DB_PASSWORD: hunter2\n"
        )
        findings = compose.lint(text)
        self.assertTrue(any("DB_PASSWORD" in f.message for f in findings))

    def test_interpolated_secret_does_not_warn(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      DB_PASSWORD: ${DB_PASSWORD}\n"
        )
        self.assertFalse(any("DB_PASSWORD" in f.message for f in compose.lint(text)))

    def test_secret_in_list_form_warns(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      - API_TOKEN=abc123\n"
        )
        self.assertTrue(any("API_TOKEN" in f.message for f in compose.lint(text)))

    def test_obsolete_version_key_is_info(self):
        text = 'version: "3.8"\nservices:\n  w:\n    image: x:1\n    restart: always\n'
        findings = compose.lint(text)
        self.assertTrue(any("obsolete" in f.message for f in findings))
        self.assertFalse(any("obsolete" in f.message and f.is_error for f in findings))

    def test_tab_indentation_is_an_error(self):
        findings = compose.lint("services:\n\tweb:\n\t\timage: x:1\n")
        self.assertTrue(any("Tab character" in f.message for f in findings))

    def test_findings_are_sorted_most_severe_first(self):
        text = 'version: "3"\nservices:\n  w:\n    image: nginx\n    ports:\n      - "1:1"\n  x:\n    ports:\n      - "1:2"\n'
        findings = compose.lint(text)
        levels = [f.level for f in findings]
        self.assertEqual(levels, sorted(levels, key=lambda l: compose._LEVEL_ORDER[l]))

    def test_every_finding_has_a_line_or_is_file_level(self):
        findings = compose.lint("services:\n  w:\n    image: nginx\n")
        for finding in findings:
            self.assertGreaterEqual(finding.line, 0)

    def test_lint_never_raises_on_odd_input(self):
        for text in ("", "   ", "services:", "services: []", "services:\n  w: null\n",
                     "a: [1, 2\n", "%YAML 1.2\n---\nservices: {}\n", "\x00\x01"):
            compose.lint(text)  # Must not raise.


@needs_yaml
class TestFindingLines(unittest.TestCase):
    """Where a finding points.

    The editor jumps the cursor to ``finding.line`` and highlights it, so an
    anchor that lands on the wrong service is worse than no anchor at all.
    :func:`compose._line_of_key` searches forward from the service's own line
    precisely because keys like ``image`` repeat in every service.
    """

    TWO_SERVICES = (
        "services:\n"
        "  a:\n    image: nginx:1.27\n    restart: always\n"
        "  b:\n    image: redis\n    restart: always\n"
    )

    def _find(self, text, needle):
        matches = [f for f in compose.lint(text) if needle in f.message]
        self.assertEqual(len(matches), 1, _messages(compose.lint(text)))
        return matches[0]

    def test_anchor_skips_the_earlier_service_with_the_same_key(self):
        finding = self._find(self.TWO_SERVICES, "'b' uses")
        self.assertEqual(finding.line, 6)  # b's own image line, not a's.

    def test_service_level_finding_points_at_the_service_key(self):
        finding = self._find(self.TWO_SERVICES, "'b' has no healthcheck")
        self.assertEqual(finding.line, 5)

    def test_version_finding_points_at_the_version_line(self):
        text = 'version: "3.8"\nservices:\n  w:\n    image: x:1\n    restart: always\n'
        self.assertEqual(self._find(text, "obsolete").line, 1)

    def test_tab_finding_points_at_the_offending_line(self):
        findings = compose.lint("services:\n  web:\n\t\timage: x:1\n")
        tabs = [f for f in findings if "Tab character" in f.message]
        self.assertEqual([f.line for f in tabs], [3])

    def test_secret_finding_points_at_the_variable(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      TZ: UTC\n      DB_PASSWORD: hunter2\n"
        )
        self.assertEqual(self._find(text, "DB_PASSWORD").line, 7)

    def test_missing_key_falls_back_to_the_service_line(self):
        # 'restart' is absent by definition, so there is no line to point at.
        finding = self._find("services:\n  w:\n    image: x:1\n", "restart policy")
        self.assertEqual(finding.line, 2)

    def test_line_of_key_ignores_a_key_used_as_a_value(self):
        self.assertEqual(compose._line_of_key("a: image\nimage: b\n", "image"), 2)

    def test_line_of_key_matches_a_key_inside_a_list_item(self):
        self.assertEqual(
            compose._line_of_key("ports:\n  - target: 80\n", "target"), 2
        )

    def test_line_of_key_with_no_match_returns_the_starting_point(self):
        self.assertEqual(compose._line_of_key("a: 1\n", "nope", after=7), 7)

    def test_line_of_key_falls_back_to_an_earlier_occurrence(self):
        # Searching forward found nothing; anywhere in the file beats nothing.
        self.assertEqual(compose._line_of_key("image: a\nb: 1\n", "image", after=2), 1)

    def test_line_of_key_with_an_empty_key(self):
        self.assertEqual(compose._line_of_key("a: 1\n", ""), 0)


class TestPortRanges(unittest.TestCase):
    """Published-port extraction for the range syntax."""

    def test_range_expands_to_every_port(self):
        self.assertEqual(
            compose._published_ports(["8000-8002:80-82"]),
            ["8000/tcp", "8001/tcp", "8002/tcp"],
        )

    @needs_yaml
    def test_range_collision_is_detected(self):
        text = (
            "services:\n"
            '  a:\n    image: x:1\n    ports:\n      - "8000-8002:80-82"\n'
            '  b:\n    image: y:1\n    ports:\n      - "8001:90"\n'
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("8001" in f.message for f in errors), _messages(errors))

    def test_range_carries_the_protocol(self):
        self.assertEqual(
            compose._published_ports(["53-54:53/udp"]), ["53/udp", "54/udp"]
        )

    def test_inverted_range_is_ignored(self):
        self.assertEqual(compose._published_ports(["9000-8000:80"]), [])

    def test_absurdly_wide_range_is_ignored(self):
        # Expanding it would add thousands of findings and help nobody.
        self.assertEqual(compose._published_ports(["1-5000:80"]), [])

    def test_non_numeric_range_is_ignored(self):
        self.assertEqual(compose._published_ports(["${FROM}-${TO}:80"]), [])

    def test_interpolated_host_port_is_ignored(self):
        self.assertEqual(compose._published_ports(["${PORT}:80"]), [])

    def test_ports_must_be_a_list(self):
        for value in (None, "8080:80", {"published": 80}, 8080):
            with self.subTest(value):
                self.assertEqual(compose._published_ports(value), [])

    def test_long_syntax_without_published_gets_an_ephemeral_port(self):
        self.assertEqual(compose._published_ports([{"target": 80}]), [])

    def test_long_syntax_protocol_defaults_to_tcp(self):
        self.assertEqual(
            compose._published_ports([{"target": 80, "published": 8080}]),
            ["8080/tcp"],
        )


@needs_yaml
class TestVolumeAndNetworkShapes(unittest.TestCase):
    """Compose accepts several shapes for these fields; all must be understood."""

    def test_long_syntax_named_volume_is_checked(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    volumes:\n      - type: volume\n        source: vol\n"
            "        target: /data\n"
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("'vol'" in f.message for f in errors), _messages(errors))

    def test_long_syntax_bind_mount_is_not_a_named_volume(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    volumes:\n      - type: bind\n        source: ./cfg\n"
            "        target: /cfg\n"
        )
        self.assertFalse(any("named volume" in f.message for f in compose.lint(text)))

    def test_anonymous_volume_needs_no_declaration(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    volumes:\n      - /data\n"
        )
        self.assertFalse(any("named volume" in f.message for f in compose.lint(text)))

    def test_volumes_declared_as_a_list(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    volumes:\n      - data:/data\n"
            "volumes:\n  - data\n"
        )
        self.assertFalse(any("named volume" in f.message for f in compose.lint(text)))

    def test_networks_in_mapping_form(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    networks:\n      missing:\n        aliases: [w]\n"
        )
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("missing" in f.message for f in errors), _messages(errors))

    def test_depends_on_as_a_bare_string(self):
        text = "services:\n  w:\n    image: x:1\n    depends_on: ghost\n"
        errors = _levels(compose.lint(text), compose.ERROR)
        self.assertTrue(any("ghost" in f.message for f in errors), _messages(errors))

    def test_environment_key_with_no_value(self):
        # 'DB_PASSWORD:' with nothing after it passes the host's value through,
        # so there is no literal in the file to warn about.
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      DB_PASSWORD:\n"
        )
        self.assertFalse(any("literal value" in f.message for f in compose.lint(text)))

    def test_environment_list_entry_without_a_value(self):
        text = (
            "services:\n  w:\n    image: x:1\n    restart: always\n"
            "    environment:\n      - DB_PASSWORD\n"
        )
        self.assertFalse(any("literal value" in f.message for f in compose.lint(text)))

    def test_service_that_is_not_a_mapping(self):
        findings = compose.lint("services:\n  w: 5\n")
        self.assertTrue(any("not a mapping" in f.message for f in findings))


class TestWithoutPyYAML(unittest.TestCase):
    """Degraded mode: PyYAML is an optional dependency.

    Without it the editor leans on the host's ``docker compose config``, so the
    contract here is that the cheap textual rules still run and nothing raises.
    """

    def setUp(self):
        patcher = mock.patch.object(compose, "YAML_AVAILABLE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_reports_neither_data_nor_error(self):
        result = compose.parse(VALID)
        self.assertIsNone(result.data)
        self.assertIsNone(result.error)
        self.assertFalse(result.ok)

    def test_textual_rules_still_run(self):
        findings = compose.lint("services:\n\tweb:\n")
        self.assertTrue(any("Tab character" in f.message for f in findings))

    def test_structural_rules_are_skipped(self):
        # nginx (untagged) would warn if the document could be parsed.
        self.assertEqual(compose.lint("services:\n  w:\n    image: nginx\n"), [])

    def test_service_names_fall_back_to_the_regex_scan(self):
        self.assertEqual(compose.service_names(VALID), ["web"])

    def test_lint_never_raises(self):
        for text in ("", "   ", "services:", "a: [1, 2\n", "\x00\x01"):
            with self.subTest(text):
                compose.lint(text)


class TestServiceNames(unittest.TestCase):
    def test_from_valid_document(self):
        self.assertEqual(compose.service_names(VALID), ["web"])

    def test_falls_back_to_regex_while_invalid(self):
        # Mid-edit files must still populate the editor's service list.
        broken = "services:\n  web:\n    image: x\n  db:\n    image: y\n   bad-indent: z\n"
        self.assertEqual(compose.service_names(broken), ["web", "db"])

    def test_no_services(self):
        self.assertEqual(compose.service_names("volumes:\n  a:\n"), [])

    def test_regex_scan_ignores_nested_block_openers(self):
        # 'ports:' and 'healthcheck:' look just like a service key one level in.
        self.assertEqual(compose._service_names_by_regex(VALID), ["web"])

    def test_regex_scan_stops_at_the_next_top_level_key(self):
        text = "services:\n  web:\n    image: x\nvolumes:\n  data:\n"
        self.assertEqual(compose._service_names_by_regex(text), ["web"])

    def test_regex_scan_allows_a_trailing_comment_on_the_service_key(self):
        text = "services:\n  web:  # the front end\n    image: x\n"
        self.assertEqual(compose._service_names_by_regex(text), ["web"])

    def test_regex_scan_skips_comment_lines(self):
        text = "services:\n  # disabled for now\n  web:\n    image: x\n"
        self.assertEqual(compose._service_names_by_regex(text), ["web"])

    def test_regex_scan_without_a_services_block(self):
        self.assertEqual(compose._service_names_by_regex("volumes:\n  a:\n"), [])


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(compose.summarize([]), "No issues found")

    def test_counts(self):
        findings = [
            compose.Finding(compose.ERROR, "a"),
            compose.Finding(compose.ERROR, "b"),
            compose.Finding(compose.WARNING, "c"),
        ]
        self.assertEqual(compose.summarize(findings), "2 errors, 1 warning")


class TestDiff(unittest.TestCase):
    def test_identical_is_empty(self):
        self.assertEqual(compose.diff("a\n", "a\n"), "")

    def test_shows_both_sides(self):
        patch = compose.diff("image: a\n", "image: b\n", "compose.yml")
        self.assertIn("-image: a", patch)
        self.assertIn("+image: b", patch)
        self.assertIn("compose.yml", patch)


class TestFinding(unittest.TestCase):
    def test_str_includes_line(self):
        self.assertIn("line 4", str(compose.Finding(compose.ERROR, "boom", 4)))

    def test_str_without_line(self):
        self.assertNotIn("line", str(compose.Finding(compose.INFO, "note", 0)))

    def test_is_error(self):
        self.assertTrue(compose.Finding(compose.ERROR, "x").is_error)
        self.assertFalse(compose.Finding(compose.WARNING, "x").is_error)


if __name__ == "__main__":
    unittest.main()
