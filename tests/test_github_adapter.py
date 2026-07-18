from __future__ import annotations

import unittest

from helpers import repository as fixture_repository
from rapp_base.github import GitHubClient, normalize_api_issue, normalize_api_repository
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import reconcile_document


class GitHubAdapterTests(unittest.TestCase):
    @staticmethod
    def raw_issue(number, title="[RAPP Base] command", body="{}"):
        return {
            "id": 100 + number,
            "node_id": f"I_{number}",
            "number": number,
            "created_at": "2026-07-18T00:00:00Z",
            "updated_at": "2026-07-18T00:00:00Z",
            "user": {"id": 4},
            "author_association": "NONE",
            "labels": [],
            "body": body,
            "state": "open",
            "title": title,
        }

    @staticmethod
    def raw_repository(
        full_name="owner/repo", repository_id=1, node_id="R_one"
    ):
        return {
            "id": repository_id,
            "node_id": node_id,
            "full_name": full_name,
        }

    def test_network_shapes_are_reduced_to_trusted_fixture_fields(self):
        repository = normalize_api_repository(
            {
                "id": 1,
                "node_id": "R_one",
                "full_name": "owner/repo",
                "private": False,
                "extra": "discarded",
            }
        )
        self.assertEqual(
            repository, {"id": 1, "node_id": "R_one", "full_name": "owner/repo"}
        )
        issue = normalize_api_issue(
            {
                "id": 2,
                "node_id": "I_two",
                "number": 3,
                "created_at": "2026-07-18T00:00:00Z",
                "updated_at": "2026-07-18T00:00:00Z",
                "user": {"id": 4, "login": "mutable-name"},
                "author_association": "NONE",
                "labels": [{"name": "rapp-base-request"}],
                "body": "{}",
                "state": "open",
                "title": "untrusted",
            }
        )
        self.assertEqual(issue["user"], {"id": 4})
        self.assertEqual(issue["title"], "untrusted")
        self.assertNotIn("login", issue["user"])

    def test_search_routes_open_prefix_issues_without_labels(self):
        def raw(number, title, *, pull_request=False):
            value = {
                "id": 100 + number,
                "node_id": f"I_{number}",
                "number": number,
                "created_at": "2026-07-18T00:00:00Z",
                "updated_at": "2026-07-18T00:00:00Z",
                "user": {"id": 4},
                "author_association": "NONE",
                "labels": [],
                "body": "{}",
                "state": "open",
                "title": title,
            }
            if pull_request:
                value["pull_request"] = {}
            return value

        paths = []
        client = GitHubClient("token", "owner/repo")

        def request(method, path, payload=None):
            paths.append((method, path, payload))
            return {
                "items": [
                    raw(1, "[RAPP Base] create resources"),
                    raw(2, "prefix [RAPP Base] unrelated"),
                    raw(3, "[RAPP Base] pull request", pull_request=True),
                ]
            }

        client.request = request
        issues = client.fetch_request_issues(limit=100)
        self.assertEqual([item["number"] for item in issues], [1])
        self.assertEqual(issues[0]["labels"], [])
        self.assertIn("/search/issues?", paths[0][1])
        self.assertIn("is%3Aopen", paths[0][1])
        self.assertNotIn("labels=", paths[0][1])

    def test_opened_event_is_admitted_when_recovery_scan_is_empty(self):
        raw_repository = self.raw_repository(
            "kody-w/rapp-base", 9001, "R_repo9001"
        )
        client = GitHubClient("token", "kody-w/rapp-base")
        client.fetch_repository = lambda: normalize_api_repository(
            raw_repository
        )
        client.fetch_request_issues = lambda *, limit: []
        document = client.fetch_reconciliation_document(
            limit=100,
            event={
                "action": "opened",
                "issue": self.raw_issue(10),
                "repository": raw_repository,
            },
        )
        with fixture_repository() as root:
            summary = reconcile_document(root, load_manifest(root), document)
            self.assertEqual(summary["admitted"], 1)
            self.assertTrue(
                (root / "state/requests/issue-110.json").is_file()
            )

    def test_unrelated_event_is_ignored(self):
        client = GitHubClient("token", "owner/repo")
        client.fetch_repository = lambda: normalize_api_repository(
            self.raw_repository()
        )
        client.fetch_request_issues = lambda *, limit: []
        document = client.fetch_reconciliation_document(
            limit=100,
            event={
                "action": "edited",
                "issue": self.raw_issue(11),
                "repository": self.raw_repository(),
            },
        )
        self.assertEqual(document["issues"], [])

    def test_event_and_scan_duplicate_is_one_first_observation(self):
        raw_repository = self.raw_repository(
            "kody-w/rapp-base", 9001, "R_repo9001"
        )
        client = GitHubClient("token", "kody-w/rapp-base")
        client.fetch_repository = lambda: normalize_api_repository(
            raw_repository
        )
        scanned = normalize_api_issue(self.raw_issue(12, body='{"later":true}'))
        client.fetch_request_issues = lambda *, limit: [scanned]
        document = client.fetch_reconciliation_document(
            limit=100,
            event={
                "action": "opened",
                "issue": self.raw_issue(12, body='{"first":true}'),
                "repository": raw_repository,
            },
        )
        self.assertEqual(len(document["issues"]), 1)
        self.assertEqual(document["issues"][0]["body"], '{"first":true}')
        with fixture_repository() as root:
            summary = reconcile_document(root, load_manifest(root), document)
            self.assertEqual(summary["admitted"], 1)
            self.assertEqual(
                len(list((root / "state/requests").glob("*.json"))), 1
            )


if __name__ == "__main__":
    unittest.main()
