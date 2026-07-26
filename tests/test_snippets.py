"""Tests for the example library.

:mod:`core.snippets` feeds two consumers that cannot check it themselves: the
editor's snippet browser inserts a body at the cursor, and
``tools/gen_compose_docs.py`` renders every field into the reference document.
A malformed entry therefore surfaces either as YAML the user has to repair by
hand or as a broken heading in the docs, so the invariants are asserted here
rather than discovered later.
"""

import unittest

from core import compose, snippets

KINDS = {"service", "fragment", "root"}

#: What each ``kind`` has to be wrapped in to become a parseable document.
_WRAPPERS = {
    "service": ("services:\n", 2),
    "fragment": ("services:\n  demo:\n", 4),
    "root": ("", 0),
}


def _as_document(snippet: snippets.Snippet) -> str:
    """The snippet as the editor would leave it in a file of its own."""
    preamble, indent = _WRAPPERS[snippet.kind]
    return preamble + snippets.reindent(snippet.body, indent)


class TestFields(unittest.TestCase):
    def test_library_is_not_empty(self):
        self.assertTrue(snippets.SNIPPETS)

    def test_required_text_is_present(self):
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                for field in ("title", "category", "summary", "body", "details"):
                    self.assertTrue(
                        getattr(snippet, field).strip(), f"{field} is empty"
                    )

    def test_kind_is_known(self):
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertIn(snippet.kind, KINDS)

    def test_category_is_declared(self):
        # An unlisted category still renders, but sorts after every declared one
        # and reads as a typo in the editor's dropdown.
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertIn(snippet.category, snippets.CATEGORIES)

    def test_every_declared_category_is_used(self):
        used = {snippet.category for snippet in snippets.SNIPPETS}
        self.assertEqual(set(snippets.CATEGORIES) - used, set())

    def test_titles_are_unique(self):
        titles = [snippet.title for snippet in snippets.SNIPPETS]
        self.assertEqual(len(titles), len(set(titles)))

    def test_summary_is_one_line(self):
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertNotIn("\n", snippet.summary.strip())

    def test_docs_url_is_https(self):
        for snippet in snippets.SNIPPETS:
            if snippet.docs_url:
                with self.subTest(snippet.title):
                    self.assertTrue(snippet.docs_url.startswith("https://"))

    def test_snippets_are_immutable(self):
        # The browser hands the same instance to every insertion.
        with self.assertRaises(AttributeError):
            snippets.SNIPPETS[0].title = "changed"


class TestBodies(unittest.TestCase):
    def test_bodies_start_at_column_zero(self):
        # reindent() prefixes every line, so a body that carries its own
        # indentation would be inserted one level too deep.
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                first = snippet.body.splitlines()[0]
                self.assertEqual(first, first.lstrip())

    def test_bodies_have_no_tabs(self):
        # A tab is an outright lint error in the file it lands in.
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertNotIn("\t", snippet.body)

    def test_bodies_have_no_trailing_whitespace(self):
        for snippet in snippets.SNIPPETS:
            for number, line in enumerate(snippet.body.splitlines(), start=1):
                if line != line.rstrip():
                    self.fail(f"{snippet.title}: trailing space on line {number}")

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_bodies_parse_where_they_belong(self):
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                result = compose.parse(_as_document(snippet))
                self.assertIsNone(result.error, f"{result.error}")

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_a_service_body_defines_exactly_one_service(self):
        for snippet in snippets.SNIPPETS:
            if snippet.kind != "service":
                continue
            with self.subTest(snippet.title):
                data = compose.parse(_as_document(snippet)).data
                self.assertEqual(len(data["services"]), 1)

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_no_body_introduces_a_syntax_error_at_any_indent(self):
        # The editor re-indents to wherever the cursor sits, and a body holding a
        # multi-line scalar would break when shifted.
        for snippet in snippets.SNIPPETS:
            if snippet.kind == "root":
                continue
            preamble, base = _WRAPPERS[snippet.kind]
            for extra in (0, 2, 4):
                text = preamble + snippets.reindent(snippet.body, base + extra)
                with self.subTest(snippet.title, indent=base + extra):
                    self.assertNotIn("syntax error", str(compose.parse(text).error))

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_no_body_ships_a_literal_secret(self):
        # The examples are meant to model interpolation, not demonstrate the
        # warning the linter raises against them.
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                literals = [
                    finding
                    for finding in compose.lint(_as_document(snippet))
                    if "literal value" in finding.message
                ]
                self.assertEqual(literals, [], f"{literals}")

    @unittest.skipUnless(compose.YAML_AVAILABLE, "PyYAML not installed")
    def test_no_service_body_uses_an_unpinned_tag(self):
        for snippet in snippets.SNIPPETS:
            if snippet.kind != "service":
                continue
            with self.subTest(snippet.title):
                unpinned = [
                    finding
                    for finding in compose.lint(_as_document(snippet))
                    if "tag" in finding.message
                ]
                self.assertEqual(unpinned, [], f"{unpinned}")


class TestByCategory(unittest.TestCase):
    def test_groups_follow_declared_order(self):
        grouped = snippets.by_category()
        expected = [name for name in snippets.CATEGORIES if name in grouped]
        self.assertEqual(list(grouped), expected)

    def test_every_snippet_appears_exactly_once(self):
        flattened = [s for items in snippets.by_category().values() for s in items]
        self.assertEqual(len(flattened), len(snippets.SNIPPETS))
        self.assertEqual(set(flattened), set(snippets.SNIPPETS))

    def test_no_empty_groups(self):
        # The browser's dropdown is built from these keys.
        for category, items in snippets.by_category().items():
            with self.subTest(category):
                self.assertTrue(items)

    def test_file_order_is_preserved_within_a_group(self):
        grouped = snippets.by_category()
        for category, items in grouped.items():
            with self.subTest(category):
                original = [s for s in snippets.SNIPPETS if s.category == category]
                self.assertEqual(items, original)


class TestFind(unittest.TestCase):
    def test_exact_title(self):
        wanted = snippets.SNIPPETS[0]
        self.assertIs(snippets.find(wanted.title), wanted)

    def test_every_title_is_findable(self):
        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertIs(snippets.find(snippet.title), snippet)

    def test_unknown_title(self):
        self.assertIsNone(snippets.find("no such snippet"))

    def test_lookup_is_case_sensitive(self):
        self.assertIsNone(snippets.find(snippets.SNIPPETS[0].title.upper() + "!"))


class TestReindent(unittest.TestCase):
    def test_zero_indent_is_unchanged(self):
        self.assertEqual(snippets.reindent("a:\n  b: 1\n", 0), "a:\n  b: 1\n")

    def test_prefixes_every_line(self):
        self.assertEqual(snippets.reindent("a:\n  b: 1\n", 2), "  a:\n    b: 1\n")

    def test_blank_lines_get_no_trailing_whitespace(self):
        self.assertEqual(snippets.reindent("a:\n\nb:\n", 4), "    a:\n\n    b:\n")

    def test_result_always_ends_with_one_newline(self):
        for body in ("a: 1", "a: 1\n", "a: 1\n\n\n"):
            with self.subTest(body):
                result = snippets.reindent(body, 2)
                self.assertTrue(result.endswith("\n"))
                self.assertFalse(result.endswith("\n\n"))

    def test_empty_body(self):
        self.assertEqual(snippets.reindent("", 4), "")

    def test_relative_indentation_is_kept(self):
        body = "a:\n  b:\n    c: 1\n"
        self.assertEqual(
            snippets.reindent(body, 2), "  a:\n    b:\n      c: 1\n"
        )


if __name__ == "__main__":
    unittest.main()
