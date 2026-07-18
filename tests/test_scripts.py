from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from helpers import (
    PROJECT_ROOT,
    REPOSITORY,
    create_command,
    issue,
    load_receipt,
    repository,
)
from rapp_base.errors import RappError
from rapp_base.jsonutil import canonical_bytes
from scripts.check import check as check_repository
from scripts.prepare_pages import DIRECTORIES, FILES, prepare


class ScriptFixtureTests(unittest.TestCase):
    def test_fixture_runs_through_local_reconcile_and_build_commands(self):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with repository() as root:
            reconcile = subprocess.run(
                [
                    sys.executable,
                    "scripts/reconcile.py",
                    "--root",
                    str(root),
                    "--input",
                    "tests/fixtures/issues.json",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
            build = subprocess.run(
                [
                    sys.executable,
                    "scripts/build.py",
                    "--root",
                    str(root),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertTrue((root / "api/v1/receipts/issue-701.json").is_file())

    def test_build_check_reports_stale_projection_without_rewriting(self):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with repository() as root:
            generate = subprocess.run(
                [sys.executable, "scripts/build.py", "--root", str(root)],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generate.returncode, 0, generate.stderr)
            target = root / "api/v1/status.json"
            stale = b'{"schema":"stale-test/1.0"}\n'
            target.write_bytes(stale)
            check = subprocess.run(
                [
                    sys.executable,
                    "scripts/build.py",
                    "--root",
                    str(root),
                    "--check",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(check.returncode, 0)
            self.assertIn("build_out_of_date", check.stderr)
            self.assertEqual(target.read_bytes(), stale)

    def test_multibyte_oversized_issue_becomes_durable_rejection(self):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with repository() as root:
            oversized = issue(801, "😀" * 17_000, fenced=False)
            valid = issue(802, create_command(802))
            fixture = root / "multibyte-issues.json"
            fixture.write_bytes(
                canonical_bytes(
                    {
                        "repository": REPOSITORY,
                        "issues": [oversized, valid],
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/reconcile.py",
                    "--root",
                    str(root),
                    "--input",
                    str(fixture),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_receipt(root, oversized)["code"], "body_too_large")
            self.assertEqual(load_receipt(root, valid)["status"], "applied")

    def test_scale_probe_builds_isolated_growth_report(self):
        protected = {
            relative: (PROJECT_ROOT / relative).read_bytes()
            for relative in ("state/head.json", "registry.json", "versions/index.json")
        }
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        caches_before = set(PROJECT_ROOT.rglob("__pycache__"))
        result = subprocess.run(
            [
                sys.executable,
                "scripts/scale_probe.py",
                "--creates",
                "2",
                "--updates",
                "1",
                "--deletes",
                "1",
                "--rejections",
                "1",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["events"], 4)
        self.assertEqual(report["requests"], 5)
        self.assertEqual(report["tombstones"], 1)
        self.assertGreater(report["files"], 0)
        self.assertGreater(report["bytes"], 0)
        self.assertGreater(len(report["largest_files"]), 0)
        self.assertGreater(len(report["largest_directories"]), 0)
        self.assertGreater(report["pages_artifact_bytes_estimate"], 0)
        self.assertIn("total", report["elapsed_seconds"])
        self.assertFalse((PROJECT_ROOT / ".scale-work").exists())
        self.assertEqual(set(PROJECT_ROOT.rglob("__pycache__")), caches_before)
        for relative, data in protected.items():
            self.assertEqual((PROJECT_ROOT / relative).read_bytes(), data)

    def test_ci_uses_event_baseline_and_never_cancels_push_checks(self):
        workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('["before"]', workflow)
        self.assertIn('["pull_request"]["base"]["sha"]', workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertNotIn("cancel-in-progress:", workflow)

    def test_repository_check_rejects_even_an_escaping_symlink(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        link = PROJECT_ROOT / ".test-escaping-link"
        try:
            os.symlink("../outside-rapp-base", link)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")
        try:
            with self.assertRaises(RappError) as raised:
                check_repository(PROJECT_ROOT)
            self.assertEqual(raised.exception.code, "symlink")
        finally:
            link.unlink(missing_ok=True)

    def test_pages_allowlist_rejects_nested_symlinks_before_copy(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        with repository() as root:
            for relative in FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("fixture\n", encoding="utf-8")
            for relative in DIRECTORIES:
                (root / relative).mkdir(parents=True, exist_ok=True)
            link = root / "sdk/escaping-link"
            try:
                os.symlink("../../outside-rapp-base", link)
            except OSError as exc:
                self.skipTest(f"cannot create symlink: {exc}")
            output = root / ".pages"
            with self.assertRaisesRegex(ValueError, "symlink"):
                prepare(output, root=root)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
