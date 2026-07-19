"""Fail-closed write fencing and bounded operator controls."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

API_BASE = "https://api.github.com"
CONTROL_PATH = ".rapp-base/write-control.json"
CONTROL_REF = "main"
CONTROL_SCHEMA = "rapp-base-write-control/1.0"
CONTROL_MAX_BYTES = 512
WORKFLOW_FILE = "process.yml"
ACTIVE_RUN_STATUSES = ("queued", "in_progress")
PAUSED_EXIT = 3
MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_BLOB_RESPONSE_BYTES = 16 * 1024
MAX_TREE_ENTRIES = 10_000
MAX_PAGES = 10
PAGE_SIZE = 100
CONTROL_UPDATE_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 15

_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class WriteControlError(Exception):
    """An operational error whose message is safe to print."""


@dataclass(frozen=True)
class ControlState:
    enabled: bool
    exists: bool
    sha: str | None


@dataclass(frozen=True)
class GateDecision:
    enabled: bool
    reason: str


@dataclass(frozen=True)
class PauseResult:
    cancel_requests: int
    polls: int


class WriteControlAdapter(Protocol):
    def read_control(self) -> ControlState: ...

    def ensure_control_enabled(self, enabled: bool) -> ControlState: ...

    def list_active_process_runs(self) -> list[dict[str, Any]]: ...

    def cancel_process_run(self, run_id: int) -> bool: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY_RE.fullmatch(value) is None:
        raise WriteControlError("GITHUB_REPOSITORY must be owner/name")
    if any(part in {".", ".."} for part in value.split("/")):
        raise WriteControlError("GITHUB_REPOSITORY is unsafe")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def control_document_bytes(enabled: bool) -> bytes:
    if not isinstance(enabled, bool):
        raise WriteControlError("control enabled value must be boolean")
    return (
        json.dumps(
            {"enabled": enabled, "schema": CONTROL_SCHEMA},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def parse_control_document(raw: bytes) -> bool:
    if not isinstance(raw, bytes):
        raise WriteControlError("control document must be bytes")
    if len(raw) > CONTROL_MAX_BYTES:
        raise WriteControlError("control document exceeds the byte limit")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise WriteControlError("control document is not strict JSON") from exc
    if not isinstance(value, dict):
        raise WriteControlError("control document must be a JSON object")
    if set(value) != {"enabled", "schema"}:
        raise WriteControlError(
            "control document must contain only schema and enabled"
        )
    if value["schema"] != CONTROL_SCHEMA:
        raise WriteControlError("control document schema is invalid")
    if not isinstance(value["enabled"], bool):
        raise WriteControlError("control document enabled must be boolean")
    return value["enabled"]


def validate_control_file(
    root: Path,
    *,
    require_canonical: bool = True,
) -> bool | None:
    path = root / CONTROL_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WriteControlError("control document cannot be read") from exc
    enabled = parse_control_document(raw)
    if require_canonical and raw != control_document_bytes(enabled):
        raise WriteControlError("control document is not canonical JSON")
    return enabled


def _git_blob_id(raw: bytes, object_id: str) -> str:
    header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
    if len(object_id) == 40:
        return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()
    if len(object_id) == 64:
        return hashlib.sha256(header + raw).hexdigest()
    raise WriteControlError("GitHub object id is invalid")


def _decode_base64_file(
    content: Any,
    *,
    expected_size: int,
    expected_sha: str,
    context: str,
) -> bytes:
    if not isinstance(content, str):
        raise WriteControlError(f"{context} content is missing")
    try:
        encoded = content.encode("ascii", errors="strict")
    except UnicodeError as exc:
        raise WriteControlError(f"{context} content is invalid base64") from exc
    max_encoded = 4 * ((CONTROL_MAX_BYTES + 2) // 3)
    max_transport = max_encoded + 2 * ((max_encoded + 59) // 60)
    if len(encoded) > max_transport:
        raise WriteControlError(f"{context} content exceeds the byte limit")
    compact = encoded.replace(b"\r", b"").replace(b"\n", b"")
    expected_encoded_size = 4 * ((expected_size + 2) // 3)
    if len(compact) != expected_encoded_size:
        raise WriteControlError(f"{context} size does not match its metadata")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WriteControlError(f"{context} content is invalid base64") from exc
    if base64.b64encode(raw) != compact:
        raise WriteControlError(f"{context} content is non-canonical base64")
    if len(raw) != expected_size:
        raise WriteControlError(f"{context} size does not match its metadata")
    if _git_blob_id(raw, expected_sha) != expected_sha:
        raise WriteControlError(f"{context} SHA does not match its content")
    return raw


class GitHubWriteControlAPI:
    """Minimal GitHub adapter restricted to fixed repository endpoints."""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        max_pages: int = MAX_PAGES,
        update_attempts: int = CONTROL_UPDATE_ATTEMPTS,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ):
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or "\r" in token
            or "\n" in token
        ):
            raise WriteControlError("GITHUB_TOKEN is missing or invalid")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_PAGES
        ):
            raise WriteControlError("API pagination bound is invalid")
        if (
            isinstance(update_attempts, bool)
            or not isinstance(update_attempts, int)
            or not 1 <= update_attempts <= CONTROL_UPDATE_ATTEMPTS
        ):
            raise WriteControlError("control update retry bound is invalid")
        self.repository = _validate_repository(repository)
        self._token = token
        self._max_pages = max_pages
        self._update_attempts = update_attempts
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open
        owner, name = self.repository.split("/", 1)
        self._repository_path = (
            f"/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}"
        )
        quoted_control = urllib.parse.quote(CONTROL_PATH, safe="/")
        self._control_path = (
            f"{self._repository_path}/contents/{quoted_control}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        expected: frozenset[int] = frozenset({200}),
        allowed_errors: frozenset[int] = frozenset(),
        byte_limit: int = MAX_API_RESPONSE_BYTES,
    ) -> tuple[int, Any]:
        if (
            method not in {"GET", "POST", "PUT"}
            or not path.startswith(self._repository_path + "/")
            or "://" in path
            or any(marker in path for marker in ("\r", "\n", "#"))
            or isinstance(byte_limit, bool)
            or not isinstance(byte_limit, int)
            or not 0 < byte_limit <= MAX_API_RESPONSE_BYTES
        ):
            raise WriteControlError("GitHub API request is unsafe")
        url = API_BASE + path
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._token,
            "User-Agent": "rapp-base-write-control/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            try:
                data = json.dumps(
                    payload,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise WriteControlError("GitHub API payload is invalid") from exc
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                if response.geturl() != url:
                    raise WriteControlError("GitHub API redirected unexpectedly")
                status = response.getcode()
                raw = response.read(byte_limit + 1)
        except WriteControlError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code if isinstance(exc.code, int) else 0
            exc.close()
            if status in allowed_errors:
                return status, None
            raise WriteControlError(
                f"GitHub API returned HTTP {status or 'error'}"
            ) from None
        except Exception:
            raise WriteControlError("GitHub API request failed") from None
        if status in allowed_errors:
            return status, None
        if status not in expected:
            raise WriteControlError(
                f"GitHub API returned unexpected HTTP {status}"
            )
        if len(raw) > byte_limit:
            raise WriteControlError("GitHub API response exceeded the byte limit")
        if not raw:
            return status, None
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise WriteControlError("GitHub API returned invalid JSON") from exc
        return status, value

    def _confirm_control_missing(self) -> None:
        query = urllib.parse.urlencode({"recursive": "1"})
        _, value = self._request(
            "GET",
            f"{self._repository_path}/git/trees/{CONTROL_REF}?{query}",
        )
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("sha"), str)
            or _OBJECT_ID_RE.fullmatch(value["sha"]) is None
            or value.get("truncated") is not False
            or not isinstance(value.get("tree"), list)
            or len(value["tree"]) > MAX_TREE_ENTRIES
        ):
            raise WriteControlError(
                "Git tree response cannot prove the control document is missing"
            )
        observed: set[str] = set()
        for entry in value["tree"]:
            if not isinstance(entry, dict):
                raise WriteControlError("Git tree response is invalid")
            path = entry.get("path")
            entry_type = entry.get("type")
            sha = entry.get("sha")
            if (
                not isinstance(path, str)
                or not path
                or len(path.encode("utf-8", errors="strict")) > 4096
                or path in observed
                or entry_type not in {"blob", "commit", "tree"}
                or not isinstance(sha, str)
                or _OBJECT_ID_RE.fullmatch(sha) is None
            ):
                raise WriteControlError("Git tree response is invalid")
            observed.add(path)
        if CONTROL_PATH in observed:
            raise WriteControlError(
                "control document exists but could not be read"
            )

    def _read_blob(self, sha: str, expected_size: int) -> bytes:
        _, value = self._request(
            "GET",
            f"{self._repository_path}/git/blobs/{sha}",
            byte_limit=MAX_BLOB_RESPONSE_BYTES,
        )
        if (
            not isinstance(value, dict)
            or value.get("sha") != sha
            or value.get("size") != expected_size
            or value.get("encoding") != "base64"
        ):
            raise WriteControlError("Git blob control response is invalid")
        return _decode_base64_file(
            value.get("content"),
            expected_size=expected_size,
            expected_sha=sha,
            context="Git blob control document",
        )

    def read_control(self) -> ControlState:
        query = urllib.parse.urlencode({"ref": CONTROL_REF})
        status, value = self._request(
            "GET",
            f"{self._control_path}?{query}",
            allowed_errors=frozenset({404}),
        )
        if status == 404:
            self._confirm_control_missing()
            return ControlState(enabled=True, exists=False, sha=None)
        if not isinstance(value, dict):
            raise WriteControlError(
                "Contents API control response is not an object"
            )
        size = value.get("size")
        sha = value.get("sha")
        if (
            value.get("type") != "file"
            or value.get("path") != CONTROL_PATH
            or value.get("name") != Path(CONTROL_PATH).name
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= CONTROL_MAX_BYTES
            or not isinstance(sha, str)
            or _OBJECT_ID_RE.fullmatch(sha) is None
        ):
            raise WriteControlError(
                "Contents API control response is invalid"
            )
        encoding = value.get("encoding")
        if encoding == "base64":
            raw = _decode_base64_file(
                value.get("content"),
                expected_size=size,
                expected_sha=sha,
                context="Contents API control document",
            )
        elif encoding == "none":
            if value.get("content") not in {"", None}:
                raise WriteControlError(
                    "Contents API control response has unexpected content"
                )
            raw = self._read_blob(sha, size)
        else:
            raise WriteControlError(
                "Contents API control encoding is invalid"
            )
        return ControlState(
            enabled=parse_control_document(raw),
            exists=True,
            sha=sha,
        )

    def _validate_update_response(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise WriteControlError("Contents API update response is invalid")
        content = value.get("content")
        commit = value.get("commit")
        if (
            not isinstance(content, dict)
            or content.get("path") != CONTROL_PATH
            or not isinstance(content.get("sha"), str)
            or _OBJECT_ID_RE.fullmatch(content["sha"]) is None
            or not isinstance(commit, dict)
            or not isinstance(commit.get("sha"), str)
            or _OBJECT_ID_RE.fullmatch(commit["sha"]) is None
        ):
            raise WriteControlError("Contents API update response is invalid")

    def ensure_control_enabled(self, enabled: bool) -> ControlState:
        if not isinstance(enabled, bool):
            raise WriteControlError("control enabled value must be boolean")
        encoded = base64.b64encode(control_document_bytes(enabled)).decode("ascii")
        for _attempt in range(self._update_attempts):
            current = self.read_control()
            if current.exists and current.enabled is enabled:
                return current
            payload: dict[str, Any] = {
                "branch": CONTROL_REF,
                "content": encoded,
                "message": (
                    "chore(control): resume writes"
                    if enabled
                    else "chore(control): pause writes"
                ),
            }
            if current.sha is not None:
                payload["sha"] = current.sha
            status, value = self._request(
                "PUT",
                self._control_path,
                payload=payload,
                expected=frozenset({200, 201}),
                allowed_errors=frozenset({409, 422}),
            )
            if status in {409, 422}:
                continue
            self._validate_update_response(value)
            confirmed = self.read_control()
            if confirmed.exists and confirmed.enabled is enabled:
                return confirmed
        raise WriteControlError(
            "control update did not stabilize within the retry limit"
        )

    def list_active_process_runs(self) -> list[dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        workflow = urllib.parse.quote(WORKFLOW_FILE, safe="")
        for requested_status in ACTIVE_RUN_STATUSES:
            for page in range(1, self._max_pages + 1):
                query = urllib.parse.urlencode(
                    {
                        "page": page,
                        "per_page": PAGE_SIZE,
                        "status": requested_status,
                    }
                )
                _, value = self._request(
                    "GET",
                    f"{self._repository_path}/actions/workflows/"
                    f"{workflow}/runs?{query}",
                )
                if not isinstance(value, dict) or not isinstance(
                    value.get("workflow_runs"), list
                ):
                    raise WriteControlError(
                        "Actions workflow run response is invalid"
                    )
                runs = value["workflow_runs"]
                if len(runs) > PAGE_SIZE:
                    raise WriteControlError(
                        "Actions workflow run page exceeds the item limit"
                    )
                for run in runs:
                    if not isinstance(run, dict):
                        raise WriteControlError(
                            "Actions workflow run item is invalid"
                        )
                    run_id = run.get("id")
                    status = run.get("status")
                    if (
                        isinstance(run_id, bool)
                        or not isinstance(run_id, int)
                        or run_id < 1
                        or status not in ACTIVE_RUN_STATUSES
                    ):
                        raise WriteControlError(
                            "Actions workflow run item is invalid"
                        )
                    result[run_id] = {"id": run_id, "status": status}
                if len(runs) < PAGE_SIZE:
                    break
                if page == self._max_pages:
                    raise WriteControlError(
                        "Actions workflow run pagination exceeded the limit"
                    )
        return [result[run_id] for run_id in sorted(result)]

    def cancel_process_run(self, run_id: int) -> bool:
        if (
            isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id < 1
        ):
            raise WriteControlError("workflow run id is invalid")
        status, _ = self._request(
            "POST",
            f"{self._repository_path}/actions/runs/{run_id}/cancel",
            expected=frozenset({202}),
            allowed_errors=frozenset({409}),
        )
        return status == 202


def _validated_control_state(state: Any) -> ControlState:
    if (
        not isinstance(state, ControlState)
        or not isinstance(state.enabled, bool)
        or not isinstance(state.exists, bool)
    ):
        raise WriteControlError("control adapter returned an invalid state")
    if state.exists:
        if (
            not isinstance(state.sha, str)
            or _OBJECT_ID_RE.fullmatch(state.sha) is None
        ):
            raise WriteControlError("control adapter returned an invalid state")
    elif state.sha is not None or state.enabled is not True:
        raise WriteControlError("control adapter returned an invalid state")
    return state


def evaluate_gate(adapter: WriteControlAdapter) -> GateDecision:
    state = _validated_control_state(adapter.read_control())
    if not state.exists:
        return GateDecision(
            True,
            "control document is missing (compatibility enabled)",
        )
    if not state.enabled:
        return GateDecision(False, "control document enabled is false")
    return GateDecision(True, "control document enabled is true")


def run_gate(
    adapter: WriteControlAdapter,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        decision = evaluate_gate(adapter)
    except WriteControlError as exc:
        print(f"write gate failed closed: {exc}", file=stderr)
        return 1
    except Exception:
        print("write gate failed closed: unexpected API failure", file=stderr)
        return 1
    if not decision.enabled:
        print(
            f"write gate: paused ({CONTROL_PATH} enabled is false)",
            file=stdout,
        )
        return PAUSED_EXIT
    print(f"write gate: enabled ({decision.reason})", file=stdout)
    return 0


def pause_processing(
    adapter: WriteControlAdapter,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> PauseResult:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 3600
        or isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not math.isfinite(poll_seconds)
        or not 0 < poll_seconds <= 60
    ):
        raise WriteControlError("pause timing bounds are invalid")
    committed = _validated_control_state(
        adapter.ensure_control_enabled(False)
    )
    if not committed.exists or committed.enabled:
        raise WriteControlError("control document was not committed as paused")
    deadline = clock() + timeout_seconds
    attempted: set[int] = set()
    cancel_requests = 0
    polls = 0
    while True:
        state = _validated_control_state(adapter.read_control())
        if not state.exists or state.enabled:
            raise WriteControlError(
                "control document is no longer committed as paused"
            )
        polls += 1
        runs = adapter.list_active_process_runs()
        if not isinstance(runs, list):
            raise WriteControlError("active workflow run response is invalid")
        if not runs:
            final_state = _validated_control_state(adapter.read_control())
            if not final_state.exists or final_state.enabled:
                raise WriteControlError(
                    "control document is no longer committed as paused"
                )
            return PauseResult(
                cancel_requests=cancel_requests,
                polls=polls,
            )
        for run in runs:
            if not isinstance(run, dict):
                raise WriteControlError("active workflow run is invalid")
            run_id = run.get("id")
            status = run.get("status")
            if (
                isinstance(run_id, bool)
                or not isinstance(run_id, int)
                or run_id < 1
                or status not in ACTIVE_RUN_STATUSES
            ):
                raise WriteControlError("active workflow run is invalid")
            if run_id not in attempted:
                attempted.add(run_id)
                if adapter.cancel_process_run(run_id):
                    cancel_requests += 1
        remaining = deadline - clock()
        if remaining <= 0:
            raise WriteControlError(
                "timed out waiting for active process.yml runs to stop"
            )
        sleeper(min(poll_seconds, remaining))


def resume_processing(adapter: WriteControlAdapter) -> None:
    committed = _validated_control_state(
        adapter.ensure_control_enabled(True)
    )
    if not committed.exists or not committed.enabled:
        raise WriteControlError("control document was not committed as enabled")
    confirmed = _validated_control_state(adapter.read_control())
    if not confirmed.exists or not confirmed.enabled:
        raise WriteControlError("control document did not remain enabled")


def _positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _bounded_pages(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= MAX_PAGES:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_PAGES}"
        )
    return parsed


def _bounded_update_attempts(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= CONTROL_UPDATE_ATTEMPTS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {CONTROL_UPDATE_ATTEMPTS}"
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check, pause, or resume RAPP Base mutation processing."
    )
    parser.add_argument("command", choices=("check", "pause", "resume"))
    parser.add_argument("--repository")
    parser.add_argument("--confirm-repository")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_number,
        default=300.0,
    )
    parser.add_argument(
        "--poll-seconds",
        type=_positive_number,
        default=2.0,
    )
    parser.add_argument("--max-pages", type=_bounded_pages, default=MAX_PAGES)
    parser.add_argument(
        "--update-attempts",
        type=_bounded_update_attempts,
        default=CONTROL_UPDATE_ATTEMPTS,
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    adapter_factory: Callable[..., WriteControlAdapter] = GitHubWriteControlAPI,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    args = _build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    repository = args.repository or environ.get("GITHUB_REPOSITORY", "")
    try:
        repository = _validate_repository(repository)
        if args.command in {"pause", "resume"} and (
            args.confirm_repository != repository
        ):
            raise WriteControlError(
                "--confirm-repository must exactly match GITHUB_REPOSITORY"
            )
        adapter = adapter_factory(
            environ.get("GITHUB_TOKEN", ""),
            repository,
            max_pages=args.max_pages,
            update_attempts=args.update_attempts,
        )
        if args.command == "check":
            return run_gate(adapter, stdout=stdout, stderr=stderr)
        if args.command == "pause":
            result = pause_processing(
                adapter,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                clock=clock,
                sleeper=sleeper,
            )
            print(
                "pause complete: "
                f"{CONTROL_PATH} enabled=false on {CONTROL_REF}, "
                f"{result.cancel_requests} cancellation request(s), "
                "no queued or in-progress process.yml runs",
                file=stdout,
            )
            return 0
        resume_processing(adapter)
        print(
            "resume complete: "
            f"{CONTROL_PATH} enabled=true on {CONTROL_REF}",
            file=stdout,
        )
        return 0
    except WriteControlError as exc:
        prefix = (
            "write gate failed closed"
            if args.command == "check"
            else "write control failed"
        )
        print(f"{prefix}: {exc}", file=stderr)
        return 1
    except Exception:
        prefix = (
            "write gate failed closed"
            if args.command == "check"
            else "write control failed"
        )
        print(f"{prefix}: unexpected API failure", file=stderr)
        return 1
