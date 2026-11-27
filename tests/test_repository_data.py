"""Test submission safeguards in a disposable repo with synthetic files only."""

import shutil
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from dlt_ai.native import ROOT
from examples.check_repository_data import blocked_path, check_repository


class PathPolicyTests(unittest.TestCase):
    def test_public_docs_do_not_link_to_private_inputs_or_reports(self):
        for name in ("README.md", "AGENTS.md", "AI_AGENT_GUIDE.md", "QUICK_START.md",
                     "CLI_GUIDE.md", "DATA_HANDLING.md", ".github/copilot-instructions.md"):
            document = ROOT / name
            text = document.read_text(encoding="utf-8")
            self.assertNotIn("\x00", text, name)
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                if target.startswith(("https://", "http://", "#")):
                    continue
                path = (document.parent / unquote(target.split("#")[0])).resolve()
                relative = path.relative_to(ROOT).as_posix()
                with self.subTest(document=name, target=target):
                    self.assertFalse(blocked_path(relative))
                    self.assertTrue(path.is_file())

    def test_sensitive_and_synthetic_paths(self):
        for name in ("artifacts/report.json", "artifacts/ANALYSIS.md", "artifacts/records.jsonl",
                     "nested/artifacts/log.txt", "real.dlt", "real.fibex", "Records.JSONL",
                     "tests/real.fibex", "examples/demo-output/report.json"):
            with self.subTest(name=name):
                self.assertTrue(blocked_path(name))
        for name in ("tests/nonverbose.fibex", "qdlt/tests/testfile.dlt", "tests/test_cli.py",
                     "examples/smoke.rules.json"):
            with self.subTest(name=name):
                self.assertFalse(blocked_path(name))


@unittest.skipUnless(shutil.which("git"), "Git required for index/ignore verification")
class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlt-synthetic-repo-")
        self.directory = Path(self.temp.name)
        self.git("init", "--quiet")
        shutil.copyfile(ROOT / ".gitignore", self.directory / ".gitignore")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.directory), *arguments], capture_output=True,
                              timeout=10, check=True)

    def test_upstream_and_private_ignore_rules_are_merged(self):
        ignored = ("build/generated.txt", "CMakeLists.txt.user", "other.user", "buildlib/library.txt",
                   ".idea/workspace.xml", "debian/file.txt", "Makefile", "compile_commands.json",
                   "__pycache__/module.pyc", "examples/demo-output/report.json",
                   "artifacts/run-001/ANALYSIS.md", "artifacts/audit.json", "artifacts/records.jsonl",
                   "real.dlt", "real.fibex", "records.jsonl")
        for name in ignored:
            with self.subTest(name=name):
                self.git("check-ignore", "--no-index", "--", name)
        for name in ("tests/nonverbose.fibex", "examples/smoke.rules.json", "dlt_ai/native.py"):
            result = subprocess.run(["git", "-C", str(self.directory), "check-ignore", "--no-index", "--", name],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1, name)

    def test_tracked_private_files_fail_even_when_force_added(self):
        report = self.directory / "artifacts" / "report.json"
        report.parent.mkdir()
        report.write_text('{"synthetic":true}', encoding="utf-8")
        self.git("add", "--force", "--", "artifacts/report.json")
        result = check_repository(self.directory)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["blocked_files"], ["artifacts/report.json"])
        self.assertTrue(report.exists())  # checker must never delete local evidence

    def test_clean_source_index_passes(self):
        fixture = self.directory / "tests" / "nonverbose.fibex"
        fixture.parent.mkdir()
        fixture.write_text("synthetic fixture", encoding="utf-8")
        self.git("add", "--", ".gitignore", "tests/nonverbose.fibex")
        self.assertEqual(check_repository(self.directory)["status"], "passed")

    def test_non_repository_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                check_repository(Path(directory))


if __name__ == "__main__":
    unittest.main()
