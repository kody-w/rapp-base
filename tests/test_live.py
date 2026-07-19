from __future__ import annotations

import base64
import copy
import hashlib
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rapp_base.constants import BASE_SCHEMA, PROFILE
from scripts.check_live import (
    GitHubLiveClient,
    HTTPAdapter,
    LiveCheckError,
    LiveConfig,
    MAX_GIT_BLOB_RESPONSE_BYTES,
    MAX_REGISTRY_BYTES,
    contents_registry_url,
    default_pages_base,
    default_raw_base,
    evaluate_snapshot,
    validate_pages_base,
    validate_raw_base,
)

NOW = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
GENERATION = "a" * 64
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((PROJECT_ROOT / "manifest.json").read_text(encoding="utf-8"))
OWNER = MANIFEST["repository"]["owner"]
NAME = MANIFEST["repository"]["name"]
REPOSITORY = f"{OWNER}/{NAME}"


def config(*, allow_no_process_run=False):
    return LiveConfig(
        repository=REPOSITORY,
        profile=PROFILE,
        raw_base=default_raw_base(REPOSITORY),
        pages_base=default_pages_base(REPOSITORY),
        token="fixture-token",
        max_command_age=timedelta(minutes=30),
        max_process_age=timedelta(hours=12),
        allow_no_process_run=allow_no_process_run,
    )


def registry(generation=GENERATION, *, clean=False):
    return {
        "schema": BASE_SCHEMA,
        "profile": PROFILE,
        "generation_sha256": generation,
        "repository": {
            "owner": OWNER,
            "name": NAME,
            "branch": "main",
        },
        "raw_base": default_raw_base(REPOSITORY),
        "pages_base": default_pages_base(REPOSITORY),
        "summary": {
            "events": 0 if clean else 3,
            "requests": 0 if clean else 3,
            "receipts": 0 if clean else 3,
        },
        "immutable_request_versions": [] if clean else [{"issue_id": 1}],
    }


def registry_payload(document):
    return json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def git_blob_sha(raw):
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
        usedforsecurity=False,
    ).hexdigest()


def contents_response(raw, *, encoding="base64"):
    return {
        "content": (
            base64.b64encode(raw).decode("ascii")
            if encoding == "base64"
            else ""
        ),
        "encoding": encoding,
        "path": "registry.json",
        "sha": git_blob_sha(raw),
        "size": len(raw),
        "type": "file",
    }


def run(
    run_id,
    *,
    created_at="2026-07-18T19:50:00Z",
    updated_at="2026-07-18T19:55:00Z",
    status="completed",
    conclusion="success",
):
    return {
        "id": run_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": status,
        "conclusion": conclusion,
    }


def snapshot(*, clean=False):
    cfg = config()
    document = registry(clean=clean)
    return {
        "registries": {
            "contents": {
                "url": contents_registry_url(REPOSITORY),
                "document": copy.deepcopy(document),
            },
            "raw": {
                "url": f"{cfg.raw_base}/registry.json",
                "document": copy.deepcopy(document),
            },
            "pages": {
                "url": f"{cfg.pages_base}registry.json",
                "document": copy.deepcopy(document),
            },
        },
        "command_issues": [],
        "process_runs": [run(1001)],
        "pages": {
            "status": "built",
            "html_url": cfg.pages_base,
        },
    }


class LiveEvaluationTests(unittest.TestCase):
    def test_matching_generations_produce_deterministic_safe_summary(self):
        summary = evaluate_snapshot(snapshot(), config(), now=NOW)
        self.assertEqual(summary["schema"], "rapp-base-live-check/1.0")
        self.assertEqual(summary["backlog"], {"count": 0, "oldest": None})
        self.assertEqual(summary["process"]["latest"]["id"], 1001)
        self.assertEqual(summary["process"]["latest_success"]["id"], 1001)
        self.assertEqual(summary["pages"]["status"], "built")
        content_digests = set()
        for source in ("contents", "pages", "raw"):
            self.assertEqual(
                summary["registries"][source]["generation_sha256"], GENERATION
            )
            digest = summary["registries"][source]["content_sha256"]
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            content_digests.add(digest)
            self.assertTrue(summary["registries"][source]["url"].startswith("https://"))
        self.assertEqual(len(content_digests), 1)

    def test_generation_mismatch_fails(self):
        value = snapshot()
        value["registries"]["pages"]["document"]["generation_sha256"] = "b" * 64
        with self.assertRaisesRegex(LiveCheckError, "generations differ"):
            evaluate_snapshot(value, config(), now=NOW)

    def test_full_registry_mismatch_fails_when_generation_matches(self):
        value = snapshot()
        value["registries"]["pages"]["document"]["summary"]["events"] += 1
        with self.assertRaisesRegex(LiveCheckError, "content digests differ"):
            evaluate_snapshot(value, config(), now=NOW)

    def test_stale_command_backlog_fails_without_retaining_title_or_body(self):
        value = snapshot()
        value["command_issues"] = [
            {
                "id": 9001,
                "number": 41,
                "created_at": "2026-07-18T19:29:00Z",
            }
        ]
        with self.assertRaisesRegex(LiveCheckError, r"Issue #41"):
            evaluate_snapshot(value, config(), now=NOW)

    def test_newest_completed_workflow_failure_fails(self):
        value = snapshot()
        value["process_runs"] = [
            run(
                1002,
                created_at="2026-07-18T19:56:00Z",
                updated_at="2026-07-18T19:59:00Z",
                conclusion="failure",
            ),
            run(1001),
        ]
        with self.assertRaisesRegex(LiveCheckError, r"1002 did not succeed"):
            evaluate_snapshot(value, config(), now=NOW)

    def test_stale_or_missing_success_fails(self):
        stale = snapshot()
        stale["process_runs"] = [
            run(
                1001,
                created_at="2026-07-18T07:00:00Z",
                updated_at="2026-07-18T07:59:00Z",
            )
        ]
        with self.assertRaisesRegex(LiveCheckError, "successful.*stale"):
            evaluate_snapshot(stale, config(), now=NOW)

        missing = snapshot()
        missing["process_runs"] = [
            run(
                1002,
                status="in_progress",
                conclusion=None,
                updated_at="2026-07-18T19:50:00Z",
            )
        ]
        with self.assertRaisesRegex(LiveCheckError, "no successful"):
            evaluate_snapshot(missing, config(), now=NOW)

    def test_allow_no_process_run_only_accepts_clean_unactivated_template(self):
        clean = snapshot(clean=True)
        clean["process_runs"] = []
        summary = evaluate_snapshot(
            clean, config(allow_no_process_run=True), now=NOW
        )
        self.assertTrue(summary["process"]["allow_no_process_run"])

        activated = snapshot()
        activated["process_runs"] = []
        with self.assertRaisesRegex(LiveCheckError, "no process.yml"):
            evaluate_snapshot(
                activated, config(allow_no_process_run=True), now=NOW
            )

    def test_pages_must_be_configured_and_built(self):
        for pages in (None, {"status": "building", "html_url": config().pages_base}):
            with self.subTest(pages=pages):
                value = snapshot()
                value["pages"] = pages
                with self.assertRaisesRegex(
                    LiveCheckError, "not configured|not built"
                ):
                    evaluate_snapshot(value, config(), now=NOW)

    def test_workflow_pages_null_api_status_is_built_by_published_registry(self):
        value = snapshot()
        value["pages"] = {
            "status": None,
            "build_type": "workflow",
            "html_url": config().pages_base,
        }
        summary = evaluate_snapshot(value, config(), now=NOW)
        self.assertIsNone(summary["pages"]["api_status"])
        self.assertEqual(summary["pages"]["build_type"], "workflow")


class LiveAdapterTests(unittest.TestCase):
    def test_contents_registry_preserves_inline_base64_fast_path(self):
        raw = registry_payload(registry())

        class Inline:
            def __init__(self):
                self.calls = []

            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return contents_response(raw)

        fake = Inline()
        item = GitHubLiveClient(fake, config()).fetch_contents_registry()
        self.assertEqual(item["document"], registry())
        self.assertEqual(len(fake.calls), 1)

    def test_large_contents_registry_uses_bounded_matching_git_blob(self):
        document = registry()
        document["padding"] = "x" * (1024 * 1024)
        raw = registry_payload(document)
        contents = contents_response(raw, encoding="none")
        blob = {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
            "sha": contents["sha"],
            "size": contents["size"],
        }

        class Large:
            def __init__(self):
                self.calls = []

            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return contents if len(self.calls) == 1 else blob

        fake = Large()
        item = GitHubLiveClient(fake, config()).fetch_contents_registry()
        self.assertEqual(item["document"], document)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn(f"/git/blobs/{contents['sha']}", fake.calls[1][0])
        self.assertEqual(
            fake.calls[1][1]["byte_limit"], MAX_GIT_BLOB_RESPONSE_BYTES
        )

    def test_large_contents_registry_rejects_sha_and_size_mismatches(self):
        raw = registry_payload(registry())
        contents = contents_response(raw, encoding="none")
        valid_blob = {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
            "sha": contents["sha"],
            "size": contents["size"],
        }
        cases = {
            "SHA": {**valid_blob, "sha": "b" * 40},
            "size": {**valid_blob, "size": contents["size"] + 1},
            "content": {
                **valid_blob,
                "content": base64.b64encode(b" " + raw[1:]).decode("ascii"),
            },
        }
        for expected, blob in cases.items():
            with self.subTest(expected=expected):
                class Mismatch:
                    def __init__(self):
                        self.calls = 0

                    def get_json(self, _url, **_kwargs):
                        self.calls += 1
                        return contents if self.calls == 1 else blob

                with self.assertRaisesRegex(
                    LiveCheckError, "SHA|size"
                ) as raised:
                    GitHubLiveClient(
                        Mismatch(), config()
                    ).fetch_contents_registry()
                if expected != "content":
                    self.assertIn(expected, str(raised.exception))

    def test_contents_registry_rejects_malformed_encodings(self):
        raw = registry_payload(registry())
        invalid_contents = contents_response(raw, encoding="gzip")
        large_contents = contents_response(raw, encoding="none")
        invalid_blob = {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "none",
            "sha": large_contents["sha"],
            "size": large_contents["size"],
        }
        malformed_blob = {
            **invalid_blob,
            "content": "!" + invalid_blob["content"][1:],
            "encoding": "base64",
        }

        class Responses:
            def __init__(self, values):
                self.values = iter(values)

            def get_json(self, _url, **_kwargs):
                return next(self.values)

        for values in ((invalid_contents,), (large_contents, invalid_blob)):
            with self.subTest(values=values):
                with self.assertRaisesRegex(LiveCheckError, "encoding"):
                    GitHubLiveClient(
                        Responses(values), config()
                    ).fetch_contents_registry()
        with self.assertRaisesRegex(LiveCheckError, "invalid base64"):
            GitHubLiveClient(
                Responses((large_contents, malformed_blob)), config()
            ).fetch_contents_registry()

    def test_large_contents_registry_enforces_byte_bound_before_blob_fetch(self):
        oversized = {
            "content": "",
            "encoding": "none",
            "path": "registry.json",
            "sha": "a" * 40,
            "size": MAX_REGISTRY_BYTES + 1,
            "type": "file",
        }

        class Oversized:
            def __init__(self):
                self.calls = 0

            def get_json(self, _url, **_kwargs):
                self.calls += 1
                return oversized

        fake = Oversized()
        with self.assertRaisesRegex(LiveCheckError, "byte limit"):
            GitHubLiveClient(fake, config()).fetch_contents_registry()
        self.assertEqual(fake.calls, 1)

    def test_issues_pagination_is_bounded(self):
        class FullPages:
            def __init__(self):
                self.urls = []

            def get_json(self, url, **_kwargs):
                self.urls.append(url)
                return [
                    {"state": "open", "title": "unrelated"}
                    for _ in range(100)
                ]

        fake = FullPages()
        client = GitHubLiveClient(fake, config(), max_pages=2)
        with self.assertRaisesRegex(LiveCheckError, "pagination"):
            client.fetch_open_command_issues()
        self.assertEqual(len(fake.urls), 2)
        self.assertIn("page=2", fake.urls[-1])

    def test_issue_scan_uses_exact_prefix_and_excludes_pull_requests(self):
        class OnePage:
            def get_json(self, _url, **_kwargs):
                return [
                    {
                        "id": 1,
                        "number": 1,
                        "created_at": "2026-07-18T19:55:00Z",
                        "state": "open",
                        "title": "[RAPP Base] create",
                    },
                    {
                        "id": 2,
                        "number": 2,
                        "created_at": "2026-07-18T19:55:00Z",
                        "state": "open",
                        "title": "question [RAPP Base]",
                    },
                    {
                        "id": 3,
                        "number": 3,
                        "created_at": "2026-07-18T19:55:00Z",
                        "state": "open",
                        "title": "[RAPP Base] pull request",
                        "pull_request": {},
                    },
                ]

        issues = GitHubLiveClient(OnePage(), config()).fetch_open_command_issues()
        self.assertEqual(issues, [{
            "created_at": "2026-07-18T19:55:00Z",
            "id": 1,
            "number": 1,
        }])

    def test_public_registry_request_is_cache_busted_but_reports_canonical_url(self):
        class Capture:
            def __init__(self):
                self.calls = []

            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return registry()

        fake = Capture()
        cfg = config()
        item = GitHubLiveClient(fake, cfg).fetch_public_registry(
            "raw", cfg.raw_base, "20260718T200000Z"
        )
        self.assertEqual(item["url"], f"{cfg.raw_base}/registry.json")
        self.assertIn("rapp_base_canary=20260718T200000Z", fake.calls[0][0])
        self.assertFalse(fake.calls[0][1]["authenticated"])

    def test_configured_urls_are_restricted_to_matching_github_origins(self):
        self.assertEqual(
            validate_raw_base(default_raw_base(REPOSITORY), REPOSITORY),
            default_raw_base(REPOSITORY),
        )
        self.assertEqual(
            validate_pages_base(default_pages_base(REPOSITORY), REPOSITORY),
            default_pages_base(REPOSITORY),
        )
        invalid = (
            lambda: validate_raw_base(
                f"http://raw.githubusercontent.com/{REPOSITORY}/main",
                REPOSITORY,
            ),
            lambda: validate_raw_base(
                f"https://example.com/{REPOSITORY}/main", REPOSITORY
            ),
            lambda: validate_pages_base(
                f"https://{OWNER}.github.io/other/", REPOSITORY
            ),
            lambda: validate_pages_base(
                f"https://{OWNER}.github.io/{NAME}/?redirect=1", REPOSITORY
            ),
            lambda: validate_pages_base(
                f"https://{OWNER}.github.io/{NAME}//", REPOSITORY
            ),
        )
        for operation in invalid:
            with self.subTest(operation=operation):
                with self.assertRaises(LiveCheckError):
                    operation()

    def test_transport_never_leaks_token_from_network_errors(self):
        token = "secret-token-that-must-not-appear"

        def fail(_request, **_kwargs):
            raise urllib.error.URLError(f"reflected {token}")

        adapter = HTTPAdapter(token, f"{OWNER}.github.io", opener=fail)
        with self.assertRaises(LiveCheckError) as raised:
            adapter.get_json(f"https://api.github.com/repos/{REPOSITORY}")
        self.assertNotIn(token, str(raised.exception))


class OperationsWorkflowTests(unittest.TestCase):
    def test_workflow_is_read_only_pinned_scheduled_and_manual(self):
        operations = (
            PROJECT_ROOT / ".github/workflows/operations.yml"
        ).read_text(encoding="utf-8")
        process = (
            PROJECT_ROOT / ".github/workflows/process.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('cron: "43 */6 * * *"', operations)
        self.assertIn('cron: "17 */6 * * *"', process)
        self.assertIn("workflow_dispatch:", operations)
        self.assertNotRegex(operations, r"(?m)^\s*push:")
        self.assertIn("contents: read", operations)
        self.assertIn("issues: read", operations)
        self.assertIn("actions: read", operations)
        self.assertIn("pages: read", operations)
        self.assertNotRegex(operations, r"(?m)^\s+\w[\w-]*:\s+write\s*$")
        self.assertIn('python-version: "3.14"', operations)

        action_lines = [
            line.strip() for line in operations.splitlines() if "uses:" in line
        ]
        self.assertEqual(len(action_lines), 2)
        for line in action_lines:
            self.assertRegex(line, r"^uses: actions/(checkout|setup-python)@[0-9a-f]{40}")
        self.assertIn("# v5", action_lines[0])
        self.assertIn("# v6", action_lines[1])

    def test_processor_file_authority_fences_reconciliation_and_each_push(self):
        process = (
            PROJECT_ROOT / ".github/workflows/process.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "jobs:\n"
            "  process:\n"
            "    runs-on: ubuntu-latest",
            process,
        )
        self.assertIn("issues:", process)
        self.assertIn("types: [opened]", process)
        self.assertIn("schedule:", process)
        self.assertIn("workflow_dispatch:", process)
        self.assertIn(
            "permissions:\n"
            "      contents: write\n"
            "      issues: write\n"
            "    steps:",
            process,
        )
        self.assertNotIn("actions: write", process)

        gate = "python3 scripts/write_control.py check"
        gate_positions = []
        position = process.find(gate)
        while position >= 0:
            gate_positions.append(position)
            position = process.find(gate, position + len(gate))
        reconcile_position = process.index("python3 scripts/reconcile.py")
        fetch_position = process.index("git fetch --no-tags origin main")
        reset_position = process.index("git reset --hard origin/main")
        commit_position = process.index(
            'git commit -m "chore(state): reconcile public issue commands"'
        )
        push_position = process.index("git push origin HEAD:main")
        self.assertTrue(
            any(position < reconcile_position for position in gate_positions)
        )
        self.assertLess(gate_positions[0], fetch_position)
        self.assertLess(fetch_position, reset_position)
        self.assertLess(reset_position, reconcile_position)
        self.assertTrue(
            any(
                commit_position < position < push_position
                for position in gate_positions
            )
        )
        self.assertIn(
            "steps.reconcile.outputs.writes_enabled == 'true'",
            process,
        )
        self.assertIn("Write gate is uncertain; refusing to push.", process)


if __name__ == "__main__":
    unittest.main()
