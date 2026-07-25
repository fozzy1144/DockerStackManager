"""Tests for compose parsing, linting, and diffing."""

import unittest

from core import compose


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


class TestLintCleanFile(unittest.TestCase):
    def test_no_errors(self):
        findings = compose.lint(VALID)
        self.assertEqual(_levels(findings, compose.ERROR), [], _messages(findings))


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


class TestServiceNames(unittest.TestCase):
    def test_from_valid_document(self):
        self.assertEqual(compose.service_names(VALID), ["web"])

    def test_falls_back_to_regex_while_invalid(self):
        # Mid-edit files must still populate the editor's service list.
        broken = "services:\n  web:\n    image: x\n  db:\n    image: y\n   bad-indent: z\n"
        self.assertEqual(compose.service_names(broken), ["web", "db"])

    def test_no_services(self):
        self.assertEqual(compose.service_names("volumes:\n  a:\n"), [])


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
