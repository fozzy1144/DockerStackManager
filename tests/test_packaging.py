"""Guards on the packaging metadata.

Dependencies are declared twice — ``requirements.txt`` for the documented
``pip install -r`` path, ``pyproject.toml`` for ``pip install .`` and the
PyInstaller build. Two lists that disagree produce a build missing a dependency
the developer has installed locally, which is the hardest kind of failure to
reproduce, so they are compared here instead.
"""

import ast
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
SPEC = ROOT / "docker-stack-manager.spec"

#: name -> lower bound, e.g. "paramiko>=3.4.0,<5" -> ("paramiko", "3.4.0")
_REQUIREMENT = re.compile(r"^([A-Za-z0-9._-]+)\s*>=\s*([0-9][0-9A-Za-z.]*)")


def _parse(lines) -> dict[str, str]:
    parsed = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQUIREMENT.match(line)
        assert match is not None, f"cannot read requirement: {line!r}"
        parsed[match.group(1).lower()] = match.group(2)
    return parsed


class TestMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with PYPROJECT.open("rb") as handle:
            cls.pyproject = tomllib.load(handle)
        cls.project = cls.pyproject["project"]

    def test_pyproject_parses(self):
        self.assertEqual(self.project["name"], "docker-stack-manager")

    def test_python_floor_matches_the_documented_one(self):
        # The README badge and this have to say the same thing.
        self.assertEqual(self.project["requires-python"], ">=3.12")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python-3.12+", readme)

    def test_entry_point_is_importable_and_callable(self):
        target = self.project["gui-scripts"]["docker-stack-manager"]
        module, _, attribute = target.partition(":")
        self.assertEqual(module, "main")
        source = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
        functions = [
            node.name
            for node in source.body
            if isinstance(node, ast.FunctionDef)
        ]
        self.assertIn(attribute, functions)

    def test_the_declared_license_ships_with_the_package(self):
        # Metadata claiming MIT with no license text is a problem for anyone
        # redistributing the wheel, and the README badge says the same thing.
        self.assertEqual(self.project["license"], "MIT")
        self.assertEqual(self.project["license-files"], ["LICENSE"])
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c)", license_text)

    def test_declared_packages_exist(self):
        for package in self.pyproject["tool"]["setuptools"]["packages"]:
            with self.subTest(package):
                self.assertTrue((ROOT / package / "__init__.py").is_file())

    def test_every_first_party_package_is_declared(self):
        # A package added later but left out here is simply absent from a wheel.
        declared = set(self.pyproject["tool"]["setuptools"]["packages"])
        found = {
            path.parent.name
            for path in ROOT.glob("*/__init__.py")
            if path.parent.name != "tests"
        }
        self.assertEqual(found - declared, set())


class TestDependenciesAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with PYPROJECT.open("rb") as handle:
            cls.declared = _parse(tomllib.load(handle)["project"]["dependencies"])
        cls.required = _parse(REQUIREMENTS.read_text(encoding="utf-8").splitlines())

    def test_same_packages(self):
        self.assertEqual(set(self.declared), set(self.required))

    def test_same_lower_bounds(self):
        self.assertEqual(self.declared, self.required)

    def test_pyproject_caps_every_dependency(self):
        # An uncapped dependency is one major release away from a broken build.
        with PYPROJECT.open("rb") as handle:
            for requirement in tomllib.load(handle)["project"]["dependencies"]:
                with self.subTest(requirement):
                    self.assertIn("<", requirement)


class TestPyInstallerSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SPEC.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_spec_is_valid_python(self):
        self.assertTrue(self.tree.body)

    def test_collects_customtkinter_data(self):
        # CustomTkinter reads its themes from disk; a frozen build without them
        # dies constructing the first widget.
        self.assertIn("collect_data_files(\"customtkinter\")", self.source)

    def test_keyring_backend_is_a_hidden_import(self):
        # Discovered through entry points, which static analysis cannot follow.
        self.assertIn("keyring.backends.Windows", self.source)

    def test_builds_a_windowed_executable(self):
        self.assertIn("console=False", self.source)

    def test_entry_script_exists(self):
        self.assertTrue((ROOT / "main.py").is_file())


if __name__ == "__main__":
    unittest.main()
