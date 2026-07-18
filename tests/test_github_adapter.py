from __future__ import annotations

import unittest

from rapp_base.github import GitHubClient, normalize_api_issue, normalize_api_repository


class GitHubAdapterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
