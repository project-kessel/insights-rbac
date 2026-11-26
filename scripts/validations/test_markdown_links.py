"""Regression tests for the offline Markdown link checker."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validations.check_markdown_links import check


class MarkdownLinksTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def test_links_anchors_references_and_ignored_code(self):
        target = self.write("other file.md", '# Hello, World!\n# Hello, World!\n<a id="custom"></a>\n')
        source = self.write(
            "source.md",
            "# Here\n[here](#here) [first](other%20file.md#hello-world) "
            "[again][duplicate] [custom](other%20file.md#custom)\n"
            "[duplicate]: other%20file.md#hello-world-1\n"
            "[external](https://example.com/not-here) [endpoint](/api/roles)\n"
            "`[not a link](missing.md)`\n```md\n[example](missing.md)\n```\n",
        )
        self.assertEqual(check([source, target]), [])

    def test_missing_files_and_anchors_reported(self):
        self.write("target.md", "# Present\n")
        source = self.write(
            "source.md", "[bad](target.md#absent) [gone](missing.md) ![image](missing.png) [self](#nope)\n"
        )
        errors = check([source])
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("target.md#absent: anchor does not exist" in error for error in errors))
        self.assertTrue(any("missing.md: target does not exist" in error for error in errors))
        self.assertTrue(any("missing.png: target does not exist" in error for error in errors))
        self.assertTrue(any("#nope: anchor does not exist" in error for error in errors))

    def test_duplicate_headings_avoid_existing_heading_ids(self):
        source = self.write("source.md", "# Title-1\n# Title\n# Title\n[duplicate](#title-2)\n")
        self.assertEqual(check([source]), [])

    def test_removing_target_breaks_unchanged_source(self):
        target = self.write("target.md", "# Title\n")
        source = self.write("source.md", "[link](target.md#title)\n")
        self.assertEqual(check([source]), [])
        target.unlink()
        self.assertEqual(len(check([source])), 1)

    def test_cli_checks_all_tracked_docs_after_target_deletion(self):
        script = self.root / "scripts/validations/check_markdown_links.py"
        script.parent.mkdir(parents=True)
        shutil.copy(Path(__file__).with_name("check_markdown_links.py"), script)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        source = self.write("docs/nested/source.md", "[target](../../target.md#title)\n")
        target = self.write("target.md", "# Title\n")
        subprocess.run(["git", "add", str(source), str(target)], cwd=self.root, check=True)
        result = subprocess.run([sys.executable, str(script)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target.unlink()
        result = subprocess.run([sys.executable, str(script)], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target does not exist", result.stdout)


if __name__ == "__main__":
    unittest.main()
