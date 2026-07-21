from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from helpers import PROJECT_ROOT
from rapp_base.build import build
from rapp_base.jsonutil import canonical_bytes
from rapp_base.manifest import load_manifest
from rapp_base.state import head_for_events
from rapp_base.write_control import CONTROL_PATH, control_document_bytes

TARGET_OWNER = "example-owner"
TARGET_REPOSITORY = "example-data"
TARGET_FULL_NAME = f"{TARGET_OWNER}/{TARGET_REPOSITORY}"
ENVIRONMENT = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
_REPOSITORY_CONTINUATION = r"A-Za-z0-9_.-"


def _copy_ignore(_directory, names):
    ignored = {
        ".git",
        ".pages",
        ".scale-work",
        ".test-work",
        ".work",
        "__pycache__",
    }
    ignored.update(
        name
        for name in names
        if name.endswith((".pyc", ".pyo")) or name.endswith((".new", ".stage"))
    )
    return ignored


@contextmanager
def full_zero_state_repository():
    scratch_parent = PROJECT_ROOT / ".test-work"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch_root = scratch_parent / f"rapp-base-bootstrap-{uuid.uuid4().hex}"
    scratch_root.mkdir()
    root = scratch_root / f"copy-{uuid.uuid4().hex}"
    try:
        shutil.copytree(PROJECT_ROOT, root, ignore=_copy_ignore)
        for name in ("requests", "receipts", "events"):
            directory = root / "state" / name
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() == ".json":
                    path.unlink()
        for name in ("api", "versions"):
            shutil.rmtree(root / name)
        (root / "registry.json").unlink()
        manifest = load_manifest(root)
        (root / "state" / "head.json").write_bytes(
            canonical_bytes(head_for_events(manifest, []))
        )
        build(root, manifest)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(scratch_root, ignore_errors=True)
        try:
            scratch_parent.rmdir()
        except OSError:
            pass


def _run(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=root,
        env=ENVIRONMENT if environment is None else environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_bootstrap(
    root: Path,
    *,
    owner: str = TARGET_OWNER,
    repository: str = TARGET_REPOSITORY,
    expose_parent_worktree: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = ENVIRONMENT
    if not expose_parent_worktree:
        environment = {
            **ENVIRONMENT,
            "GIT_CEILING_DIRECTORIES": str(root.parent),
        }
    return _run(
        root,
        "scripts/bootstrap.py",
        "--root",
        str(root),
        f"--owner={owner}",
        f"--repo={repository}",
        environment=environment,
    )


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _generated_snapshot(root: Path) -> dict[str, bytes]:
    paths = [root / "registry.json", root / "state" / "head.json"]
    for name in ("api", "versions"):
        paths.extend(path for path in (root / name).rglob("*") if path.is_file())
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(paths)
    }


def _old_reference_paths(root: Path, owner: str, repository: str) -> list[str]:
    full_name = f"{owner}/{repository}"
    values = (
        f"https://{owner}.github.io/{repository}",
        f"https://raw.githubusercontent.com/{full_name}/main",
        f"https://github.com/{full_name}",
        full_name,
    )
    patterns = tuple(
        re.compile(
            rf"(?<![{_REPOSITORY_CONTINUATION}])"
            rf"{re.escape(value)}"
            rf"(?![{_REPOSITORY_CONTINUATION}])"
        )
        for value in values
    )
    stale = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        if any(pattern.search(text) is not None for pattern in patterns):
            stale.append(path.relative_to(root).as_posix())
    return stale


class BootstrapTests(unittest.TestCase):
    def test_clean_template_control_is_reset_true_without_identity_rewrite(self):
        for initial in ("false", "missing"):
            with self.subTest(initial=initial), full_zero_state_repository() as root:
                path = root / CONTROL_PATH
                if initial == "false":
                    path.write_bytes(control_document_bytes(False))
                else:
                    path.unlink()
                result = _run_bootstrap(root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(path.read_bytes(), control_document_bytes(True))
                self.assertEqual(
                    set(json.loads(path.read_text(encoding="utf-8"))),
                    {"enabled", "schema"},
                )

    def test_refuses_malformed_template_control_without_mutation(self):
        with full_zero_state_repository() as root:
            path = root / CONTROL_PATH
            path.write_text(
                '{"enabled":true,"schema":"rapp-base-write-control/1.0",'
                f'"repository":"{"kody-w"}/{"rapp-base"}"}}\n',
                encoding="utf-8",
            )
            before = _tree_snapshot(root)
            result = _run_bootstrap(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid_write_control", result.stderr)
            self.assertEqual(_tree_snapshot(root), before)

    def test_full_zero_state_copy_becomes_a_coherent_deployment(self):
        with full_zero_state_repository() as root:
            before_manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8")
            )
            old_owner = before_manifest["repository"]["owner"]
            old_repository = before_manifest["repository"]["name"]
            old_github = f"https://github.com/{old_owner}/{old_repository}"
            new_github = f"https://github.com/{TARGET_FULL_NAME}"
            old_pages = f"https://{old_owner}.github.io/{old_repository}/"
            new_pages = (
                f"https://{TARGET_OWNER}.github.io/{TARGET_REPOSITORY}/"
            )
            clone_fixture = root / "SECURITY.md"
            clone_fixture.write_text(
                clone_fixture.read_text(encoding="utf-8")
                + f"\nClone: {old_github}.git or git@github.com:"
                f"{old_owner}/{old_repository}.git\n",
                encoding="utf-8",
            )
            future_endpoint = root / "api" / "v2" / "future.json"
            future_endpoint.parent.mkdir(parents=True, exist_ok=True)
            future_endpoint.write_text(
                '{"schema":"rapp-base-future/2.0"}\n',
                encoding="utf-8",
            )

            result = _run_bootstrap(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "CLAUDE.md").exists())
            self.assertFalse((root / "HANDOFF.md").exists())
            summary = json.loads(result.stdout)
            self.assertEqual(
                summary["from"], f"{old_owner}/{old_repository}"
            )
            self.assertEqual(summary["to"], TARGET_FULL_NAME)

            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8")
            )
            expected_manifest = copy.deepcopy(before_manifest)
            expected_manifest["repository"] = {
                "owner": TARGET_OWNER,
                "name": TARGET_REPOSITORY,
                "branch": "main",
            }
            homepage_replacements = {
                old_github: new_github,
                f"{old_github}/": f"{new_github}/",
                old_pages: new_pages,
                old_pages.removesuffix("/"): new_pages.removesuffix("/"),
            }
            for collection in expected_manifest["collections"]:
                for seed in collection["seed"]:
                    homepage = seed["data"].get("homepage")
                    if homepage in homepage_replacements:
                        seed["data"]["homepage"] = homepage_replacements[homepage]
            self.assertEqual(manifest, expected_manifest)
            self.assertEqual(manifest["profile"], "rapp-base/1.0")
            self.assertEqual(manifest["schema"], "rapp-base-manifest/1.0")
            self.assertEqual(
                manifest["collections"][0]["seed"][1]["data"]["homepage"],
                "https://github.com/kody-w/rapp-static-apis",
            )
            self.assertEqual(
                json.loads(
                    (root / "sdk" / "package.json").read_text(encoding="utf-8")
                )["name"],
                "@rapp-base/sdk",
            )

            registry = json.loads(
                (root / "registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry["repository"], manifest["repository"])
            self.assertEqual(
                registry["raw_base"],
                "https://raw.githubusercontent.com/"
                f"{TARGET_FULL_NAME}/main",
            )
            self.assertEqual(registry["pages_base"], new_pages)
            self.assertEqual(
                registry["capabilities"]["write"]["issue_url"],
                f"{new_github}/issues/new?template=rapp-base-command.yml",
            )
            self.assertEqual(registry["profile"], "rapp-base/1.0")

            for path in sorted((root / "schemas").glob("*.json")):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    schema["$id"],
                    f"{new_pages}schemas/{path.name}",
                )
            issue_config = (
                root / ".github" / "ISSUE_TEMPLATE" / "config.yml"
            ).read_text(encoding="utf-8")
            self.assertIn(f"{new_github}/issues/new", issue_config)
            self.assertIn(f"{new_github}/discussions", issue_config)
            security = clone_fixture.read_text(encoding="utf-8")
            self.assertIn(f"{new_github}.git", security)
            self.assertIn(
                f"git@github.com:{TARGET_FULL_NAME}.git",
                security,
            )
            self.assertEqual(
                future_endpoint.read_text(encoding="utf-8"),
                '{"schema":"rapp-base-future/2.0"}\n',
            )
            for relative in (
                "tests/fixtures/issues.json",
                "tests/helpers.py",
                "tests/test_delivery.py",
                "tests/test_github_adapter.py",
            ):
                self.assertIn(
                    TARGET_FULL_NAME,
                    (root / relative).read_text(encoding="utf-8"),
                )
            self.assertEqual(
                _old_reference_paths(root, old_owner, old_repository),
                [],
            )

            generated = _generated_snapshot(root)
            second_build = _run(root, "scripts/build.py", "--root", str(root))
            self.assertEqual(second_build.returncode, 0, second_build.stderr)
            self.assertEqual(json.loads(second_build.stdout)["changed"], 0)
            self.assertEqual(_generated_snapshot(root), generated)

            for pattern in ("test_delivery.py", "test_github_adapter.py"):
                tests = _run(
                    root,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    pattern,
                    "-v",
                )
                self.assertEqual(tests.returncode, 0, tests.stderr)

            reconcile = _run(
                root,
                "scripts/reconcile.py",
                "--root",
                str(root),
                "--input",
                "tests/fixtures/issues.json",
            )
            self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
            self.assertEqual(json.loads(reconcile.stdout)["applied"], 1)
            rebuilt = _run(root, "scripts/build.py", "--root", str(root))
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertTrue(
                (root / "api" / "v1" / "receipts" / "issue-701.json").is_file()
            )
            repository_check = _run(
                root,
                "scripts/check.py",
                "--root",
                str(root),
            )
            self.assertEqual(
                repository_check.returncode,
                0,
                repository_check.stderr,
            )
            self.assertEqual(
                _old_reference_paths(root, old_owner, old_repository),
                [],
            )

    def test_refuses_each_kind_of_admitted_state_without_mutation(self):
        for name in ("requests", "receipts", "events"):
            with self.subTest(state=name), full_zero_state_repository() as root:
                blocked = root / "state" / name / "blocked.json"
                blocked.write_text("{}\n", encoding="utf-8")
                before = _tree_snapshot(root)
                result = _run_bootstrap(root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("admitted_state", result.stderr)
                self.assertEqual(_tree_snapshot(root), before)

    def test_refuses_nonzero_head_without_mutation(self):
        with full_zero_state_repository() as root:
            head_path = root / "state" / "head.json"
            head = json.loads(head_path.read_text(encoding="utf-8"))
            head["sequence"] = 1
            head_path.write_bytes(canonical_bytes(head))
            before = _tree_snapshot(root)
            result = _run_bootstrap(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("admitted_state", result.stderr)
            self.assertEqual(_tree_snapshot(root), before)

    def test_refuses_invalid_github_names_without_mutation(self):
        cases = (
            ("", TARGET_REPOSITORY),
            ("bad/owner", TARGET_REPOSITORY),
            ("-leading", TARGET_REPOSITORY),
            ("trailing-", TARGET_REPOSITORY),
            ("double--hyphen", TARGET_REPOSITORY),
            ("a" * 40, TARGET_REPOSITORY),
            (TARGET_OWNER, ""),
            (TARGET_OWNER, ".."),
            (TARGET_OWNER, "bad/repository"),
            (TARGET_OWNER, "bad repository"),
            (TARGET_OWNER, "a" * 101),
        )
        with full_zero_state_repository() as root:
            before = _tree_snapshot(root)
            for owner, repository in cases:
                with self.subTest(owner=owner, repository=repository):
                    result = _run_bootstrap(
                        root,
                        owner=owner,
                        repository=repository,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("bootstrap refused", result.stderr)
                    self.assertEqual(_tree_snapshot(root), before)

    def test_refuses_symlinks_without_mutation(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        with full_zero_state_repository() as root:
            link = root / "bootstrap-link"
            try:
                os.symlink("README.md", link)
            except OSError as exc:
                self.skipTest(f"cannot create symlink: {exc}")
            before = _tree_snapshot(root)
            result = _run_bootstrap(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr)
            self.assertEqual(_tree_snapshot(root), before)

    def test_refuses_copy_nested_inside_an_ancestor_worktree(self):
        nested_parent = PROJECT_ROOT / ".test-work"
        nested_root = nested_parent / f"bootstrap-{uuid.uuid4().hex}"
        try:
            shutil.copytree(PROJECT_ROOT, nested_root, ignore=_copy_ignore)
            for name in ("requests", "receipts", "events"):
                directory = nested_root / "state" / name
                for path in directory.glob("*.json"):
                    path.unlink()
            for name in ("api", "versions"):
                shutil.rmtree(nested_root / name)
            (nested_root / "registry.json").unlink()
            manifest = load_manifest(nested_root)
            (nested_root / "state" / "head.json").write_bytes(
                canonical_bytes(head_for_events(manifest, []))
            )
            build(nested_root, manifest)
            result = _run_bootstrap(
                nested_root,
                expose_parent_worktree=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nested inside another Git worktree", result.stderr)
        finally:
            shutil.rmtree(nested_parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
