from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import unittest
import urllib.error
import uuid
from pathlib import Path

from helpers import PROJECT_ROOT
from rapp_base.write_control import (
    API_BASE,
    CONTROL_PATH,
    CONTROL_REF,
    CONTROL_SCHEMA,
    PAUSED_EXIT,
    ControlState,
    GitHubWriteControlAPI,
    WriteControlError,
    control_document_bytes,
    main,
    parse_control_document,
    pause_processing,
    resume_processing,
    run_gate,
    validate_control_file,
)

REPOSITORY = "owner/repo"
TOKEN = "github-token-fixture"
SHA_A = "a" * 40
SHA_B = "b" * 40
COMMIT_SHA = "c" * 40


def git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
        usedforsecurity=False,
    ).hexdigest()


def contents_value(raw: bytes, *, encoding: str = "base64") -> dict:
    return {
        "content": (
            base64.b64encode(raw).decode("ascii")
            if encoding == "base64"
            else ""
        ),
        "encoding": encoding,
        "name": "write-control.json",
        "path": CONTROL_PATH,
        "sha": git_blob_sha(raw),
        "size": len(raw),
        "type": "file",
    }


def tree_value(*paths: str) -> dict:
    return {
        "sha": SHA_A,
        "tree": [
            {
                "mode": "100644",
                "path": path,
                "sha": SHA_B,
                "type": "blob",
            }
            for path in paths
        ],
        "truncated": False,
    }


class Response:
    def __init__(self, url: str, status: int, value=None):
        self.url = url
        self.status = status
        self.raw = (
            b""
            if value is None
            else json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.raw[:limit]


def http_error(request, status: int):
    return urllib.error.HTTPError(
        request.full_url,
        status,
        "fixture",
        {},
        io.BytesIO(),
    )


class GateTests(unittest.TestCase):
    def test_control_schema_is_versioned_strict_and_bounded(self):
        self.assertTrue(parse_control_document(control_document_bytes(True)))
        self.assertFalse(parse_control_document(control_document_bytes(False)))
        invalid = (
            b"[]",
            b'{"enabled":true}',
            (
                b'{"enabled":true,"schema":"'
                + CONTROL_SCHEMA.encode()
                + b'","extra":1}'
            ),
            b'{"enabled":"false","schema":"rapp-base-write-control/1.0"}',
            b'{"enabled":true,"enabled":false,"schema":"rapp-base-write-control/1.0"}',
            b'{"enabled":true,"schema":"rapp-base-write-control/2.0"}',
            b'{"enabled":true,"schema":"rapp-base-write-control/1.0"} trailing',
            b"{" + (b" " * 600) + b"}",
        )
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                with self.assertRaises(WriteControlError):
                    parse_control_document(raw)

    def test_local_file_validation_allows_missing_but_requires_canonical_schema(self):
        root = PROJECT_ROOT / ".test-work" / f"control-{uuid.uuid4().hex}"
        try:
            root.mkdir(parents=True)
            self.assertIsNone(validate_control_file(root))
            path = root / CONTROL_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(control_document_bytes(False))
            self.assertFalse(validate_control_file(root))
            path.write_text(
                json.dumps(
                    {"schema": CONTROL_SCHEMA, "enabled": False},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WriteControlError, "canonical"):
                validate_control_file(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
            try:
                (PROJECT_ROOT / ".test-work").rmdir()
            except OSError:
                pass

    def test_false_is_paused_and_missing_is_compatibly_enabled(self):
        false = type(
            "FalseControl",
            (),
            {
                "read_control": lambda self: ControlState(
                    enabled=False,
                    exists=True,
                    sha=SHA_A,
                )
            },
        )()
        output = io.StringIO()
        self.assertEqual(
            run_gate(false, stdout=output, stderr=io.StringIO()),
            PAUSED_EXIT,
        )
        self.assertIn("paused", output.getvalue())

        missing = type(
            "MissingControl",
            (),
            {
                "read_control": lambda self: ControlState(
                    enabled=True,
                    exists=False,
                    sha=None,
                )
            },
        )()
        output = io.StringIO()
        self.assertEqual(
            run_gate(missing, stdout=output, stderr=io.StringIO()),
            0,
        )
        self.assertIn("missing", output.getvalue())

    def test_github_token_contents_read_uses_only_fixed_main_url(self):
        raw = control_document_bytes(True)
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(
                request.full_url,
                f"{API_BASE}/repos/owner/repo/contents/"
                ".rapp-base/write-control.json?ref=main",
            )
            self.assertEqual(
                request.get_header("Authorization"),
                f"Bearer {TOKEN}",
            )
            return Response(request.full_url, 200, contents_value(raw))

        adapter = GitHubWriteControlAPI(TOKEN, REPOSITORY, opener=opener)
        stdout = io.StringIO()
        self.assertEqual(
            run_gate(adapter, stdout=stdout, stderr=io.StringIO()),
            0,
        )
        self.assertEqual(len(requests), 1)

    def test_missing_file_is_enabled_only_after_complete_main_tree_proof(self):
        urls = []

        def opener(request, **_kwargs):
            urls.append(request.full_url)
            if "/contents/" in request.full_url:
                raise http_error(request, 404)
            self.assertEqual(
                request.full_url,
                f"{API_BASE}/repos/owner/repo/git/trees/main?recursive=1",
            )
            return Response(request.full_url, 200, tree_value("README.md"))

        adapter = GitHubWriteControlAPI(TOKEN, REPOSITORY, opener=opener)
        state = adapter.read_control()
        self.assertEqual(
            state,
            ControlState(enabled=True, exists=False, sha=None),
        )
        self.assertEqual(len(urls), 2)

    def test_ambiguous_missing_or_malformed_document_fails_closed(self):
        def truncated(request, **_kwargs):
            if "/contents/" in request.full_url:
                raise http_error(request, 404)
            value = tree_value("README.md")
            value["truncated"] = True
            return Response(request.full_url, 200, value)

        stderr = io.StringIO()
        status = run_gate(
            GitHubWriteControlAPI(TOKEN, REPOSITORY, opener=truncated),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(status, 1)
        self.assertIn("failed closed", stderr.getvalue())

        malformed = b'{"enabled":"false","schema":"rapp-base-write-control/1.0"}'

        def malformed_response(request, **_kwargs):
            return Response(
                request.full_url,
                200,
                contents_value(malformed),
            )

        stderr = io.StringIO()
        status = run_gate(
            GitHubWriteControlAPI(
                TOKEN,
                REPOSITORY,
                opener=malformed_response,
            ),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(status, 1)
        self.assertIn("enabled must be boolean", stderr.getvalue())

    def test_bounded_git_blob_fallback_is_supported(self):
        raw = control_document_bytes(False)
        sha = git_blob_sha(raw)
        urls = []

        def opener(request, **_kwargs):
            urls.append(request.full_url)
            if "/contents/" in request.full_url:
                return Response(
                    request.full_url,
                    200,
                    contents_value(raw, encoding="none"),
                )
            self.assertEqual(
                request.full_url,
                f"{API_BASE}/repos/owner/repo/git/blobs/{sha}",
            )
            return Response(
                request.full_url,
                200,
                {
                    "content": base64.b64encode(raw).decode("ascii"),
                    "encoding": "base64",
                    "sha": sha,
                    "size": len(raw),
                },
            )

        state = GitHubWriteControlAPI(
            TOKEN,
            REPOSITORY,
            opener=opener,
        ).read_control()
        self.assertFalse(state.enabled)
        self.assertEqual(len(urls), 2)

    def test_network_errors_fail_closed_without_logging_token(self):
        token = "secret-token-that-must-not-appear"

        def fail(_request, **_kwargs):
            raise urllib.error.URLError(f"reflected {token}")

        stderr = io.StringIO()
        status = run_gate(
            GitHubWriteControlAPI(token, REPOSITORY, opener=fail),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(status, 1)
        self.assertIn("failed closed", stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())


class ContentsUpdateTests(unittest.TestCase):
    def test_missing_control_is_created_on_main_without_a_sha(self):
        enabled = control_document_bytes(True)
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise http_error(request, 404)
            if len(requests) == 2:
                return Response(request.full_url, 200, tree_value("README.md"))
            if len(requests) == 3:
                body = json.loads(request.data)
                self.assertEqual(request.get_method(), "PUT")
                self.assertEqual(body["branch"], CONTROL_REF)
                self.assertNotIn("sha", body)
                self.assertEqual(
                    base64.b64decode(body["content"]),
                    enabled,
                )
                return Response(
                    request.full_url,
                    201,
                    {
                        "commit": {"sha": COMMIT_SHA},
                        "content": {
                            "path": CONTROL_PATH,
                            "sha": git_blob_sha(enabled),
                        },
                    },
                )
            return Response(
                request.full_url,
                200,
                contents_value(enabled),
            )

        state = GitHubWriteControlAPI(
            TOKEN,
            REPOSITORY,
            opener=opener,
        ).ensure_control_enabled(True)
        self.assertTrue(state.exists)
        self.assertTrue(state.enabled)
        self.assertEqual(len(requests), 4)

    def test_update_uses_current_sha_fixed_main_and_canonical_content(self):
        original = control_document_bytes(True)
        updated = control_document_bytes(False)
        updated_sha = git_blob_sha(updated)
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            if len(requests) == 1:
                return Response(
                    request.full_url,
                    200,
                    contents_value(original),
                )
            if len(requests) == 2:
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(request.get_method(), "PUT")
                self.assertEqual(
                    request.full_url,
                    f"{API_BASE}/repos/owner/repo/contents/{CONTROL_PATH}",
                )
                self.assertEqual(body["branch"], CONTROL_REF)
                self.assertEqual(body["sha"], git_blob_sha(original))
                self.assertEqual(
                    base64.b64decode(body["content"]),
                    updated,
                )
                return Response(
                    request.full_url,
                    200,
                    {
                        "commit": {"sha": COMMIT_SHA},
                        "content": {
                            "path": CONTROL_PATH,
                            "sha": updated_sha,
                        },
                    },
                )
            return Response(
                request.full_url,
                200,
                contents_value(updated),
            )

        state = GitHubWriteControlAPI(
            TOKEN,
            REPOSITORY,
            opener=opener,
        ).ensure_control_enabled(False)
        self.assertTrue(state.exists)
        self.assertFalse(state.enabled)
        self.assertEqual(len(requests), 3)

    def test_update_conflict_refetches_and_retries_with_new_sha(self):
        first = control_document_bytes(True)
        second = b'{"schema":"rapp-base-write-control/1.0","enabled":true}\n'
        final = control_document_bytes(False)
        put_shas = []
        step = [0]

        def opener(request, **_kwargs):
            step[0] += 1
            if step[0] == 1:
                return Response(request.full_url, 200, contents_value(first))
            if step[0] == 2:
                put_shas.append(json.loads(request.data)["sha"])
                raise http_error(request, 409)
            if step[0] == 3:
                return Response(request.full_url, 200, contents_value(second))
            if step[0] == 4:
                put_shas.append(json.loads(request.data)["sha"])
                return Response(
                    request.full_url,
                    200,
                    {
                        "commit": {"sha": COMMIT_SHA},
                        "content": {
                            "path": CONTROL_PATH,
                            "sha": git_blob_sha(final),
                        },
                    },
                )
            return Response(request.full_url, 200, contents_value(final))

        state = GitHubWriteControlAPI(
            TOKEN,
            REPOSITORY,
            opener=opener,
        ).ensure_control_enabled(False)
        self.assertFalse(state.enabled)
        self.assertEqual(
            put_shas,
            [git_blob_sha(first), git_blob_sha(second)],
        )

    def test_update_conflicts_stop_at_the_configured_retry_bound(self):
        raw = control_document_bytes(True)
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.get_method())
            if request.get_method() == "GET":
                return Response(request.full_url, 200, contents_value(raw))
            raise http_error(request, 409)

        adapter = GitHubWriteControlAPI(
            TOKEN,
            REPOSITORY,
            opener=opener,
            update_attempts=2,
        )
        with self.assertRaisesRegex(WriteControlError, "retry limit"):
            adapter.ensure_control_enabled(False)
        self.assertEqual(calls, ["GET", "PUT", "GET", "PUT"])

class OperatorTests(unittest.TestCase):
    def test_pause_commits_false_then_cancels_and_waits(self):
        class Fake:
            def __init__(self):
                self.calls = []
                self.snapshots = iter(
                    (
                        [
                            {"id": 11, "status": "in_progress"},
                            {"id": 12, "status": "queued"},
                        ],
                        [
                            {"id": 11, "status": "in_progress"},
                            {"id": 13, "status": "queued"},
                        ],
                        [],
                    )
                )

            def ensure_control_enabled(self, enabled):
                self.calls.append(("commit", enabled))
                return ControlState(enabled=enabled, exists=True, sha=SHA_A)

            def read_control(self):
                self.calls.append(("confirm",))
                return ControlState(enabled=False, exists=True, sha=SHA_A)

            def list_active_process_runs(self):
                self.calls.append(("list",))
                return next(self.snapshots)

            def cancel_process_run(self, run_id):
                self.calls.append(("cancel", run_id))
                return True

        now = [0.0]
        sleeps = []

        def clock():
            return now[0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        fake = Fake()
        result = pause_processing(
            fake,
            timeout_seconds=30,
            poll_seconds=1,
            clock=clock,
            sleeper=sleep,
        )
        self.assertEqual(
            fake.calls[:2],
            [("commit", False), ("confirm",)],
        )
        self.assertEqual(
            [call for call in fake.calls if call[0] == "cancel"],
            [("cancel", 11), ("cancel", 12), ("cancel", 13)],
        )
        self.assertEqual(
            len([call for call in fake.calls if call[0] == "confirm"]),
            4,
        )
        self.assertEqual(sleeps, [1, 1])
        self.assertEqual(result.cancel_requests, 3)
        self.assertEqual(result.polls, 3)

    def test_pause_does_not_complete_if_control_changes_or_listing_fails(self):
        class Changed:
            def ensure_control_enabled(self, enabled):
                return ControlState(enabled=enabled, exists=True, sha=SHA_A)

            def read_control(self):
                return ControlState(enabled=True, exists=True, sha=SHA_B)

        with self.assertRaisesRegex(WriteControlError, "no longer"):
            pause_processing(
                Changed(),
                timeout_seconds=30,
                poll_seconds=1,
            )

        class Failed(Changed):
            def read_control(self):
                return ControlState(enabled=False, exists=True, sha=SHA_A)

            def list_active_process_runs(self):
                raise WriteControlError("GitHub API returned HTTP 503")

        adapter = Failed()

        def factory(_token, _repository, **_kwargs):
            return adapter

        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(
            ["pause", "--confirm-repository", REPOSITORY],
            environ={
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_TOKEN": TOKEN,
            },
            adapter_factory=factory,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(status, 1)
        self.assertNotIn("pause complete", stdout.getvalue())
        self.assertIn("HTTP 503", stderr.getvalue())

    def test_resume_commits_and_confirms_true(self):
        class Fake:
            def __init__(self):
                self.calls = []

            def ensure_control_enabled(self, enabled):
                self.calls.append(("commit", enabled))
                return ControlState(enabled=True, exists=True, sha=SHA_A)

            def read_control(self):
                self.calls.append(("confirm",))
                return ControlState(enabled=True, exists=True, sha=SHA_A)

        fake = Fake()
        resume_processing(fake)
        self.assertEqual(
            fake.calls,
            [("commit", True), ("confirm",)],
        )

    def test_operator_requires_exact_repository_confirmation(self):
        class Fake:
            def __init__(self):
                self.calls = []

            def ensure_control_enabled(self, enabled):
                self.calls.append(enabled)
                return ControlState(enabled=enabled, exists=True, sha=SHA_A)

            def read_control(self):
                return ControlState(enabled=True, exists=True, sha=SHA_A)

        adapter = Fake()

        def factory(_token, _repository, **_kwargs):
            return adapter

        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": TOKEN,
        }
        rejected = main(
            ["resume"],
            environ=environment,
            adapter_factory=factory,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rejected, 1)
        self.assertEqual(adapter.calls, [])

        stdout = io.StringIO()
        resumed = main(
            ["resume", "--confirm-repository", REPOSITORY],
            environ=environment,
            adapter_factory=factory,
            stdout=stdout,
            stderr=io.StringIO(),
        )
        self.assertEqual(resumed, 0)
        self.assertEqual(adapter.calls, [True])
        self.assertIn("resume complete", stdout.getvalue())
        self.assertIn("enabled=true", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
