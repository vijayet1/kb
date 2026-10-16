"""Pure-logic tests for scripts/release.py (the end-to-end run is exercised against a throwaway clone by hand)."""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

### Added
- something new

## [1.0.0] - 2026-01-01

### Added
- first
"""


class Bump(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(release.bump_version("1.6.0", "patch"), "1.6.1")
        self.assertEqual(release.bump_version("1.6.9", "minor"), "1.7.0")
        self.assertEqual(release.bump_version("1.6.9", "major"), "2.0.0")

    def test_explicit_version_must_be_newer_and_valid(self):
        self.assertEqual(release.bump_version("1.6.0", "1.10.0"), "1.10.0")
        for bad in ("1.6.0", "1.5.9", "banana", "1.2", "v1.7.0"):
            with self.assertRaises(release.ReleaseError, msg=bad):
                release.bump_version("1.6.0", bad)

    def test_numeric_not_lexicographic_comparison(self):
        self.assertEqual(release.bump_version("1.9.0", "1.10.0"), "1.10.0")


class Changelog(unittest.TestCase):
    def test_unreleased_body(self):
        self.assertEqual(release.unreleased_body(CHANGELOG), "### Added\n- something new")

    def test_promote_keeps_a_fresh_unreleased_on_top(self):
        out = release.promote_changelog(CHANGELOG, "1.1.0", "2026-02-02")
        self.assertIn("## [Unreleased]\n\n## [1.1.0] - 2026-02-02\n\n### Added\n- something new", out)
        self.assertIn("## [1.0.0] - 2026-01-01", out)
        self.assertEqual(release.unreleased_body(out), "")  # next release must describe its own changes

    def test_empty_or_missing_unreleased_blocks_a_release(self):
        for text in ("# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - x\n- a\n", "# Changelog\n## [1.0.0] - x\n"):
            with self.assertRaises(release.ReleaseError):
                release.promote_changelog(text, "1.1.0", "2026-02-02")

    def test_unreleased_as_last_section(self):
        self.assertEqual(release.unreleased_body("## [Unreleased]\n\n- only\n"), "- only")


if __name__ == "__main__":
    unittest.main()
