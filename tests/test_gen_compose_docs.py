"""Guards on the generated compose reference.

``docs/compose-reference.md`` is built from :mod:`core.snippets`. Nothing stops
someone editing the library and committing without re-running the generator, and
the result is a reference document that quietly disagrees with what the editor
inserts. This test is that stop.

Run ``python tools/gen_compose_docs.py`` to fix a failure here.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "tools" / "gen_compose_docs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_compose_docs", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGeneratedDocsAreCurrent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = _load_generator()

    def test_committed_file_matches_the_generator(self):
        self.assertEqual(
            self.generator.OUTPUT.read_text(encoding="utf-8"),
            self.generator.build(),
            "docs/compose-reference.md is stale — "
            "run: python tools/gen_compose_docs.py",
        )

    def test_generation_is_deterministic(self):
        self.assertEqual(self.generator.build(), self.generator.build())


class TestAnchors(unittest.TestCase):
    """The contents list links to each heading by a generated anchor."""

    @classmethod
    def setUpClass(cls):
        cls.generator = _load_generator()

    def test_anchors_are_unique(self):
        from core import snippets

        anchors = [self.generator._anchor(s.title) for s in snippets.SNIPPETS]
        duplicates = {a for a in anchors if anchors.count(a) > 1}
        self.assertEqual(duplicates, set())

    def test_anchors_are_not_empty(self):
        from core import snippets

        for snippet in snippets.SNIPPETS:
            with self.subTest(snippet.title):
                self.assertTrue(self.generator._anchor(snippet.title))

    def test_anchor_drops_punctuation_and_lowercases(self):
        self.assertEqual(self.generator._anchor("Health & lifecycle"), "health--lifecycle")

    def test_every_contents_link_resolves_to_a_heading(self):
        import re

        text = self.generator.build()
        headings = {
            self.generator._anchor(match.group(1))
            for match in re.finditer(r"^#{2,3} (.+)$", text, re.MULTILINE)
        }
        links = set(re.findall(r"\]\(#([^)]+)\)", text))
        self.assertEqual(links - headings, set())


if __name__ == "__main__":
    unittest.main()
