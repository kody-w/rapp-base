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
from rapp_base.write_control import CONTROL_PATH, control_document_bytes
from scripts.check import check as check_repository, check_control_document
from scripts.pages_deployment import decide_pages_deployment
from scripts.prepare_pages import DIRECTORIES, FILES, prepare


class ScriptFixtureTests(unittest.TestCase):
    def test_repository_check_validates_optional_control_document(self):
        with repository() as root:
            check_control_document(root)
            path = root / CONTROL_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(control_document_bytes(True))
            check_control_document(root)
            path.write_text(
                '{"enabled":"true","schema":"rapp-base-write-control/1.0"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(RappError) as raised:
                check_control_document(root)
            self.assertEqual(
                raised.exception.code,
                "invalid_write_control",
            )

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

    def test_pages_deployment_decision_handles_manual_ci_and_processor_runs(self):
        before = "a" * 40
        current = "b" * 40
        manual = decide_pages_deployment(
            event_name="workflow_dispatch",
            current_sha=current,
        )
        ci = decide_pages_deployment(
            event_name="workflow_run",
            current_sha=current,
            workflow_name="Read-only CI",
            workflow_event="push",
            workflow_conclusion="success",
            workflow_head_sha=before,
        )
        no_commit = decide_pages_deployment(
            event_name="workflow_run",
            current_sha=before,
            workflow_name="Process RAPP Base requests",
            workflow_event="schedule",
            workflow_conclusion="success",
            workflow_head_sha=before,
        )
        changed = decide_pages_deployment(
            event_name="workflow_run",
            current_sha=current,
            workflow_name="Process RAPP Base requests",
            workflow_event="issues",
            workflow_conclusion="success",
            workflow_head_sha=before,
        )
        self.assertTrue(manual.deploy)
        self.assertEqual(manual.reason, "manual_dispatch")
        self.assertTrue(ci.deploy)
        self.assertEqual(ci.reason, "successful_ci_push")
        self.assertFalse(no_commit.deploy)
        self.assertEqual(no_commit.reason, "processor_no_commit")
        self.assertTrue(changed.deploy)
        self.assertEqual(changed.reason, "processor_changed_main")
        no_op_cli = subprocess.run(
            [
                sys.executable,
                "scripts/pages_deployment.py",
                "--event-name",
                "workflow_run",
                "--current-sha",
                before,
                "--workflow-name",
                "Process RAPP Base requests",
                "--workflow-conclusion",
                "success",
                "--workflow-head-sha",
                before,
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(no_op_cli.returncode, 0, no_op_cli.stderr)
        self.assertIn("deploy=false\n", no_op_cli.stdout)
        self.assertIn("reason=processor_no_commit\n", no_op_cli.stdout)

    def test_later_processor_noop_does_not_replace_historical_needed_deploy(self):
        before = "a" * 40
        current = "b" * 40
        historical_needed = decide_pages_deployment(
            event_name="workflow_run",
            current_sha=current,
            workflow_name="Process RAPP Base requests",
            workflow_event="issues",
            workflow_conclusion="success",
            workflow_head_sha=before,
        )
        later_noop = decide_pages_deployment(
            event_name="workflow_run",
            current_sha=current,
            workflow_name="Process RAPP Base requests",
            workflow_event="schedule",
            workflow_conclusion="success",
            workflow_head_sha=current,
        )
        self.assertTrue(historical_needed.deploy)
        self.assertEqual(historical_needed.reason, "processor_changed_main")
        self.assertFalse(later_noop.deploy)
        self.assertEqual(later_noop.reason, "processor_no_commit")

    def test_pages_workflow_isolates_decision_from_deploy_concurrency(self):
        workflow = (PROJECT_ROOT / ".github/workflows/pages.yml").read_text(
            encoding="utf-8"
        )
        decision_start = workflow.index("  decision:\n")
        deploy_start = workflow.index("  deploy:\n")
        preamble = workflow[:decision_start]
        decision = workflow[decision_start:deploy_start]
        deploy = workflow[deploy_start:]

        self.assertNotIn("concurrency:", preamble)
        self.assertNotIn("concurrency:", decision)
        self.assertEqual(workflow.count("concurrency:"), 1)
        self.assertIn(
            "    concurrency:\n"
            "      group: pages\n"
            "      cancel-in-progress: true",
            deploy,
        )
        self.assertIn("    needs: decision\n", deploy)
        self.assertIn(
            "    if: ${{ needs.decision.outputs.deploy == 'true' }}\n",
            deploy,
        )
        self.assertIn(
            "      deploy: ${{ steps.deployment-decision.outputs.deploy }}",
            decision,
        )

        decision_steps = (
            "- name: Check out current main for decision",
            "- name: Determine deployment need",
        )
        decision_positions = [decision.index(step) for step in decision_steps]
        self.assertEqual(decision_positions, sorted(decision_positions))
        self.assertNotIn("- name: Set up Python", decision)
        decision_block = decision[decision_positions[1] :]
        decision_run = decision_block.split("run: |", 1)[1]
        self.assertNotIn("${{", decision_run)
        self.assertIn(
            "WORKFLOW_RUN_HEAD_SHA: "
            "${{ github.event.workflow_run.head_sha || '' }}",
            decision_block,
        )
        self.assertIn("--current-sha \"$(git rev-parse HEAD)\"", decision_run)
        self.assertIn(
            '--workflow-head-sha "${WORKFLOW_RUN_HEAD_SHA}"',
            decision_run,
        )
        self.assertIn("GITHUB_STEP_SUMMARY", decision_run)

        deploy_steps = (
            "- name: Check out current main for deployment",
            "- name: Set up Python",
            "- name: Verify and prepare",
            "- name: Configure Pages",
            "- name: Upload Pages artifact",
            "- name: Deploy Pages",
        )
        deploy_positions = [deploy.index(step) for step in deploy_steps]
        self.assertEqual(deploy_positions, sorted(deploy_positions))
        verify = deploy[deploy_positions[2] : deploy_positions[3]]
        self.assertIn("ref: main", deploy[: deploy_positions[1]])
        self.assertIn("make check", verify)
        self.assertIn("python3 scripts/prepare_pages.py --output .pages", verify)

        self.assertNotIn("pages:", preamble + decision)
        self.assertNotIn("id-token:", preamble + decision)
        self.assertEqual(workflow.count("pages: write"), 1)
        self.assertEqual(workflow.count("id-token: write"), 1)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)

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
