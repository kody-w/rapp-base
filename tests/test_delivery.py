from __future__ import annotations

import base64
import unittest

from rapp_base.errors import RappError
from rapp_base.manifest import load_manifest
from scripts.deliver_receipts import _comment_body, deliver_receipts

from helpers import create_command, issue, load_receipt, reconcile, repository


class FakeGitHub:
    def __init__(self, root, issues):
        self.root = root
        self.repository = "kody-w/rapp-base"
        self.issues = issues
        self.comments = {}
        self.calls = []
        self.unreachable = set()

    def fetch_request_issues(self, *, limit):
        return self.issues[:limit]

    def get_all(self, path, *, limit, truncate=False):
        number = int(path.split("/")[-2])
        return self.comments.get(number, [])[:limit]

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if "/contents/" in path:
            relative = path.split("/contents/", 1)[1].split("?", 1)[0]
            issue_id = int(relative.rsplit("issue-", 1)[1].split(".json", 1)[0])
            if issue_id in self.unreachable:
                raise RappError("github_api", "simulated reachability failure")
            return {
                "content": base64.b64encode((self.root / relative).read_bytes()).decode(),
                "encoding": "base64",
            }
        return {}


class DeliveryTests(unittest.TestCase):
    def test_user_marker_spoof_does_not_suppress_trusted_exact_comment(self):
        with repository() as root:
            value = issue(201, create_command(201))
            reconcile(root, [value])
            receipt = load_receipt(root, value)
            state_path = f"state/receipts/issue-{value['id']}.json"
            expected = _comment_body(receipt, "kody-w/rapp-base", state_path)
            client = FakeGitHub(root, [value])
            client.comments[value["number"]] = [
                {
                    "body": expected + "\nspoofed suffix",
                    "user": {"login": "untrusted-user"},
                }
            ]
            delivered, failures = deliver_receipts(
                root,
                load_manifest(root),
                client,
            )
            self.assertEqual((delivered, failures), (1, []))
            posts = [call for call in client.calls if call[0] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0][2]["body"], expected)
            patches = [call for call in client.calls if call[0] == "PATCH"]
            self.assertEqual(
                patches,
                [
                    (
                        "PATCH",
                        f"/repos/kody-w/rapp-base/issues/{value['number']}",
                        {"labels": [], "state": "closed"},
                    )
                ],
            )

    def test_exact_trusted_comment_is_idempotent(self):
        with repository() as root:
            value = issue(202, create_command(202))
            reconcile(root, [value])
            receipt = load_receipt(root, value)
            state_path = f"state/receipts/issue-{value['id']}.json"
            client = FakeGitHub(root, [value])
            client.comments[value["number"]] = [
                {
                    "body": _comment_body(
                        receipt,
                        "kody-w/rapp-base",
                        state_path,
                    ),
                    "user": {"login": "github-actions[bot]"},
                }
            ]
            delivered, failures = deliver_receipts(
                root,
                load_manifest(root),
                client,
            )
            self.assertEqual((delivered, failures), (1, []))
            self.assertFalse(any(call[0] == "POST" for call in client.calls))

    def test_one_poisoned_receipt_does_not_block_later_delivery(self):
        with repository() as root:
            first = issue(203, create_command(203))
            second = issue(204, create_command(204))
            reconcile(root, [first, second])
            client = FakeGitHub(root, [first, second])
            client.unreachable.add(first["id"])
            delivered, failures = deliver_receipts(
                root,
                load_manifest(root),
                client,
            )
            self.assertEqual(delivered, 1)
            self.assertEqual(len(failures), 1)
            self.assertIn(f"Issue #{first['number']}", failures[0])
            self.assertTrue(
                any(
                    call[0] == "PATCH"
                    and call[1].endswith(f"/issues/{second['number']}")
                    for call in client.calls
                )
            )


if __name__ == "__main__":
    unittest.main()
