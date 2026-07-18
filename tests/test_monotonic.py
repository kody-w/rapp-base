from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from helpers import PROJECT_ROOT
from rapp_base.jsonutil import canonical_bytes
from scripts.check_monotonic import MonotonicError, _blobs, check_monotonic


@contextmanager
def git_repository():
    parent = PROJECT_ROOT / ".test-work"
    root = parent / f"git-{uuid.uuid4().hex}"
    root.mkdir(parents=True)

    def git(*arguments):
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("config", "user.name", "RAPP Base Tests")
    git("config", "user.email", "tests@example.com")
    version = canonical_bytes({"schema": "version/1.0", "value": 1})
    version_hash = hashlib.sha256(version).hexdigest()
    files = {
        "state/events/00000001-base.json": canonical_bytes({"event": 1}),
        "state/requests/issue-1.json": canonical_bytes({"request": 1}),
        "state/receipts/issue-1.json": canonical_bytes({"receipt": 1}),
        "state/head.json": canonical_bytes(
            {
                "event_hash": "e" * 64,
                "event_path": "00000001-base.json",
                "genesis_sha256": "a" * 64,
                "schema": "rapp-base-head/1.0",
                "sequence": 1,
            }
        ),
        f"versions/records/example/one/{version_hash[:12]}.json": version,
        "versions/index.json": canonical_bytes(
            {
                "entries": [
                    {
                        "content_sha256": version_hash,
                        "kind": "record",
                        "path": (
                            "versions/records/example/one/"
                            f"{version_hash[:12]}.json"
                        ),
                        "semantic_sha256": "b" * 64,
                    }
                ],
                "generation_sha256": "c" * 64,
                "schema": "rapp-base-version-index/1.0",
                "totalItems": 1,
            }
        ),
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            parent.rmdir()
        except OSError:
            pass


class MonotonicHistoryTests(unittest.TestCase):
    def test_large_blob_batch_is_drained_without_pipe_deadlock(self):
        with git_repository() as root:
            object_id = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "HEAD:state/requests/issue-1.json",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            objects = {
                f"state/requests/synthetic-{index}.json": object_id
                for index in range(5_000)
            }
            blobs = _blobs(root, objects)
            self.assertEqual(len(blobs), 5_000)
            self.assertEqual(len(set(blobs.values())), 1)

    def test_code_only_and_append_only_changes_are_permitted(self):
        with git_repository() as root:
            (root / "README.md").write_text("code/docs-only change\n")
            summary = check_monotonic(root, "HEAD")
            self.assertEqual(summary["current_sequence"], 1)

            (root / "state/events/00000002-next.json").write_bytes(
                canonical_bytes({"event": 2})
            )
            (root / "state/requests/issue-2.json").write_bytes(
                canonical_bytes({"request": 2})
            )
            (root / "state/receipts/issue-2.json").write_bytes(
                canonical_bytes({"receipt": 2})
            )
            head = json.loads((root / "state/head.json").read_text())
            head.update(
                {
                    "event_hash": "f" * 64,
                    "event_path": "00000002-next.json",
                    "sequence": 2,
                }
            )
            (root / "state/head.json").write_bytes(canonical_bytes(head))
            index_path = root / "versions/index.json"
            index = json.loads(index_path.read_text())
            extra = canonical_bytes({"schema": "version/1.0", "value": 2})
            digest = hashlib.sha256(extra).hexdigest()
            relative = f"versions/requests/{digest[:12]}.json"
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(extra)
            index["entries"].append(
                {
                    "content_sha256": digest,
                    "kind": "request",
                    "path": relative,
                    "semantic_sha256": "d" * 64,
                }
            )
            index["totalItems"] = 2
            index_path.write_bytes(canonical_bytes(index))
            summary = check_monotonic(root, "HEAD")
            self.assertEqual(summary["current_sequence"], 2)

    def test_prior_immutable_change_or_removal_is_rejected(self):
        with git_repository() as root:
            event = root / "state/events/00000001-base.json"
            event.write_bytes(canonical_bytes({"event": "changed"}))
            with self.assertRaisesRegex(MonotonicError, "immutable file changed"):
                check_monotonic(root, "HEAD")

        with git_repository() as root:
            (root / "state/requests/issue-1.json").unlink()
            with self.assertRaisesRegex(MonotonicError, "prior path was removed"):
                check_monotonic(root, "HEAD")

    def test_genesis_sequence_and_prior_version_entries_are_monotonic(self):
        with git_repository() as root:
            head_path = root / "state/head.json"
            head = json.loads(head_path.read_text())
            head["genesis_sha256"] = "9" * 64
            head_path.write_bytes(canonical_bytes(head))
            with self.assertRaisesRegex(MonotonicError, "genesis changed"):
                check_monotonic(root, "HEAD")

        with git_repository() as root:
            head_path = root / "state/head.json"
            head = json.loads(head_path.read_text())
            head["sequence"] = 0
            head_path.write_bytes(canonical_bytes(head))
            with self.assertRaisesRegex(MonotonicError, "sequence decreased"):
                check_monotonic(root, "HEAD")

        with git_repository() as root:
            index_path = root / "versions/index.json"
            index = json.loads(index_path.read_text())
            index["entries"] = []
            index["totalItems"] = 0
            index_path.write_bytes(canonical_bytes(index))
            with self.assertRaisesRegex(MonotonicError, "entry changed or disappeared"):
                check_monotonic(root, "HEAD")


if __name__ == "__main__":
    unittest.main()
