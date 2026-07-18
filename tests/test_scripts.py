from __future__ import annotations

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
from rapp_base.jsonutil import canonical_bytes


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


if __name__ == "__main__":
    unittest.main()
