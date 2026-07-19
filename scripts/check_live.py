#!/usr/bin/env python3
"""Check the hosted RAPP Base boundary without changing repository state."""

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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.constants import BASE_SCHEMA, REQUEST_TITLE_PREFIX
from rapp_base.errors import RappError
from rapp_base.manifest import load_manifest

API_BASE = "https://api.github.com"
LIVE_SCHEMA = "rapp-base-live-check/1.0"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_REGISTRY_BYTES + 2) // 3)
MAX_BASE64_TRANSPORT_CHARS = MAX_BASE64_CHARS + (
    2 * ((MAX_BASE64_CHARS + 59) // 60)
)
MAX_GIT_BLOB_RESPONSE_BYTES = MAX_BASE64_TRANSPORT_CHARS + (64 * 1024)
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 300_000
MAX_OBJECT_KEYS = 512
MAX_ARRAY_ITEMS = 50_000
MAX_PAGES = 10
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 10
FUTURE_TOLERANCE_SECONDS = 300

_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_RUN_VALUE_RE = re.compile(r"^[a-z_]{1,40}$")


class LiveCheckError(Exception):
    """A concise operational failure safe to print in workflow logs."""


@dataclass(frozen=True)
class LiveConfig:
    repository: str
    profile: str
    raw_base: str
    pages_base: str
    token: str = field(repr=False)
    max_command_age: timedelta
    max_process_age: timedelta
    allow_no_process_run: bool

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise LiveCheckError("JSON response exceeds the node limit")
        if depth > MAX_JSON_DEPTH:
            raise LiveCheckError("JSON response exceeds the depth limit")
        if isinstance(current, dict):
            if len(current) > MAX_OBJECT_KEYS:
                raise LiveCheckError("JSON object exceeds the key limit")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if len(current) > MAX_ARRAY_ITEMS:
                raise LiveCheckError("JSON array exceeds the item limit")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise LiveCheckError("JSON response contains a non-finite number")


def _decode_json(raw: bytes, *, byte_limit: int) -> Any:
    if len(raw) > byte_limit:
        raise LiveCheckError("response exceeds the byte limit")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveCheckError("response is not strict JSON") from exc
    _validate_json_shape(value)
    return value


def _canonical_json_digest(value: Any) -> str:
    try:
        canonical = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LiveCheckError("registry cannot be serialized canonically") from exc
    return hashlib.sha256(canonical).hexdigest()


def _git_blob_sha(raw: bytes) -> str:
    header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _decode_registry_base64(
    content: Any,
    *,
    expected_size: int,
    expected_sha: str,
    context: str,
) -> Any:
    if not isinstance(content, str):
        raise LiveCheckError(f"{context} content is missing")
    try:
        encoded = content.encode("ascii", errors="strict")
    except UnicodeError as exc:
        raise LiveCheckError(f"{context} has invalid base64") from exc
    if len(encoded) > MAX_BASE64_TRANSPORT_CHARS:
        raise LiveCheckError(f"{context} exceeds the byte limit")
    compact = encoded.replace(b"\r", b"").replace(b"\n", b"")
    expected_encoded_size = 4 * ((expected_size + 2) // 3)
    if len(compact) != expected_encoded_size:
        raise LiveCheckError(f"{context} size does not match its metadata")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LiveCheckError(f"{context} has invalid base64") from exc
    if base64.b64encode(raw) != compact:
        raise LiveCheckError(f"{context} has non-canonical base64")
    if len(raw) != expected_size:
        raise LiveCheckError(f"{context} size does not match its metadata")
    if _git_blob_sha(raw) != expected_sha:
        raise LiveCheckError(f"{context} SHA does not match its content")
    return _decode_json(raw, byte_limit=MAX_REGISTRY_BYTES)


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY_RE.fullmatch(value) is None:
        raise LiveCheckError("repository must be owner/name")
    if any(part in {".", ".."} for part in value.split("/")):
        raise LiveCheckError("repository is unsafe")
    return value


def _split_base_url(
    value: str, context: str, *, allow_query: bool = False
) -> urllib.parse.SplitResult:
    if not isinstance(value, str) or len(value) > 500:
        raise LiveCheckError(f"{context} is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LiveCheckError(f"{context} is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or (parsed.query and not allow_query)
        or parsed.fragment
        or not parsed.hostname
    ):
        raise LiveCheckError(f"{context} must be a fixed HTTPS origin")
    return parsed


def validate_raw_base(value: str, repository: str) -> str:
    owner, name = validate_repository(repository).split("/", 1)
    parsed = _split_base_url(value, "raw base")
    expected_path = f"/{owner}/{name}/main"
    if (
        parsed.hostname != "raw.githubusercontent.com"
        or parsed.netloc != "raw.githubusercontent.com"
        or parsed.path not in {expected_path, expected_path + "/"}
    ):
        raise LiveCheckError("raw base does not match the repository main branch")
    return f"https://raw.githubusercontent.com{expected_path}"


def validate_pages_base(value: str, repository: str) -> str:
    owner, name = validate_repository(repository).split("/", 1)
    parsed = _split_base_url(value, "Pages base")
    expected_host = f"{owner.lower()}.github.io"
    expected_path = "/" if name.lower() == expected_host else f"/{name}/"
    allowed_paths = {"", "/"} if expected_path == "/" else {
        expected_path,
        expected_path.rstrip("/"),
    }
    if (
        parsed.hostname != expected_host
        or parsed.netloc.lower() != expected_host
        or parsed.path not in allowed_paths
    ):
        raise LiveCheckError("Pages base does not match the repository Pages site")
    return f"https://{expected_host}{expected_path}"


def default_raw_base(repository: str) -> str:
    owner, name = validate_repository(repository).split("/", 1)
    return f"https://raw.githubusercontent.com/{owner}/{name}/main"


def default_pages_base(repository: str) -> str:
    owner, name = validate_repository(repository).split("/", 1)
    host = f"{owner.lower()}.github.io"
    path = "/" if name.lower() == host else f"/{name}/"
    return f"https://{host}{path}"


def contents_registry_url(repository: str) -> str:
    owner, name = validate_repository(repository).split("/", 1)
    owner = urllib.parse.quote(owner, safe="")
    name = urllib.parse.quote(name, safe="")
    return f"{API_BASE}/repos/{owner}/{name}/contents/registry.json?ref=main"


def _parse_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise LiveCheckError(f"{context} has an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LiveCheckError(f"{context} has an invalid timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


class HTTPAdapter:
    """Bounded stdlib HTTP transport restricted to the required GitHub origins."""

    def __init__(
        self,
        token: str,
        pages_host: str,
        *,
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
            raise LiveCheckError("GITHUB_TOKEN is missing or invalid")
        self._token = token
        self._pages_host = pages_host
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def _validate_url(self, url: str) -> str:
        parsed = _split_base_url(url, "request URL", allow_query=True)
        allowed = {
            "api.github.com",
            "raw.githubusercontent.com",
            self._pages_host,
        }
        if parsed.hostname not in allowed or parsed.netloc.lower() != parsed.hostname:
            raise LiveCheckError("request URL is outside the fixed GitHub origins")
        return parsed.hostname

    def get_json(
        self,
        url: str,
        *,
        authenticated: bool = True,
        byte_limit: int = MAX_RESPONSE_BYTES,
    ) -> Any:
        host = self._validate_url(url)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "rapp-base-live-check/1.0",
        }
        if authenticated:
            if host != "api.github.com":
                raise LiveCheckError("credentials are restricted to the GitHub API")
            headers["Authorization"] = f"Bearer {self._token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with self._opener(request, timeout=self._timeout) as response:
                final_url = response.geturl()
                if final_url != url:
                    raise LiveCheckError("HTTP redirects are not allowed")
                raw = response.read(byte_limit + 1)
        except LiveCheckError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code if isinstance(exc.code, int) else "error"
            exc.close()
            raise LiveCheckError(f"GitHub request returned HTTP {code}") from None
        except Exception:
            raise LiveCheckError("GitHub request failed") from None
        return _decode_json(raw, byte_limit=byte_limit)


class GitHubLiveClient:
    """HTTP collection layer; evaluation remains independent and deterministic."""

    def __init__(
        self,
        http: HTTPAdapter,
        config: LiveConfig,
        *,
        max_pages: int = MAX_PAGES,
    ):
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise LiveCheckError("pagination bound is invalid")
        self.http = http
        self.config = config
        self.max_pages = max_pages

    def _api(
        self,
        path: str,
        *,
        authenticated: bool = True,
        byte_limit: int = MAX_RESPONSE_BYTES,
    ) -> Any:
        if not path.startswith("/") or "://" in path or "\r" in path or "\n" in path:
            raise LiveCheckError("GitHub API path is unsafe")
        return self.http.get_json(
            API_BASE + path,
            authenticated=authenticated,
            byte_limit=byte_limit,
        )

    def fetch_contents_registry(self) -> dict[str, Any]:
        url = contents_registry_url(self.config.repository)
        value = self.http.get_json(url)
        if not isinstance(value, dict):
            raise LiveCheckError("Contents API registry response is not an object")
        if (
            value.get("type") != "file"
            or value.get("path") != "registry.json"
        ):
            raise LiveCheckError("Contents API registry response is incomplete")
        size = value.get("size")
        sha = value.get("sha")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise LiveCheckError("Contents API registry size is invalid")
        if size > MAX_REGISTRY_BYTES:
            raise LiveCheckError("Contents API registry exceeds the byte limit")
        if not isinstance(sha, str) or _GIT_SHA_RE.fullmatch(sha) is None:
            raise LiveCheckError("Contents API registry SHA is invalid")
        encoding = value.get("encoding")
        if encoding == "base64":
            document = _decode_registry_base64(
                value.get("content"),
                expected_size=size,
                expected_sha=sha,
                context="Contents API registry",
            )
        elif encoding == "none":
            if value.get("content") not in {"", None}:
                raise LiveCheckError(
                    "Contents API registry has unexpected unencoded content"
                )
            owner, name = self.config.repository.split("/", 1)
            owner = urllib.parse.quote(owner, safe="")
            name = urllib.parse.quote(name, safe="")
            blob = self._api(
                f"/repos/{owner}/{name}/git/blobs/{sha}",
                byte_limit=MAX_GIT_BLOB_RESPONSE_BYTES,
            )
            if not isinstance(blob, dict):
                raise LiveCheckError("Git blob registry response is not an object")
            if blob.get("sha") != sha:
                raise LiveCheckError(
                    "Git blob registry SHA does not match the Contents API"
                )
            if blob.get("size") != size:
                raise LiveCheckError(
                    "Git blob registry size does not match the Contents API"
                )
            if blob.get("encoding") != "base64":
                raise LiveCheckError("Git blob registry encoding is invalid")
            document = _decode_registry_base64(
                blob.get("content"),
                expected_size=size,
                expected_sha=sha,
                context="Git blob registry",
            )
        else:
            raise LiveCheckError("Contents API registry encoding is invalid")
        return {
            "document": document,
            "url": url,
        }

    def fetch_public_registry(self, source: str, base: str, nonce: str) -> dict[str, Any]:
        if source not in {"raw", "pages"}:
            raise LiveCheckError("registry source is invalid")
        canonical_url = f"{base.rstrip('/')}/registry.json"
        separator = "&" if "?" in canonical_url else "?"
        request_url = (
            canonical_url
            + separator
            + urllib.parse.urlencode({"rapp_base_canary": nonce})
        )
        return {
            "document": self.http.get_json(
                request_url,
                authenticated=False,
                byte_limit=MAX_REGISTRY_BYTES,
            ),
            "url": canonical_url,
        }

    def fetch_open_command_issues(self) -> list[dict[str, Any]]:
        owner, name = self.config.repository.split("/", 1)
        prefix = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        result: list[dict[str, Any]] = []
        for page in range(1, self.max_pages + 1):
            query = urllib.parse.urlencode(
                {
                    "direction": "asc",
                    "page": page,
                    "per_page": PAGE_SIZE,
                    "sort": "created",
                    "state": "open",
                }
            )
            values = self._api(f"{prefix}/issues?{query}")
            if not isinstance(values, list) or len(values) > PAGE_SIZE:
                raise LiveCheckError("Issues API page has an invalid shape")
            for value in values:
                if not isinstance(value, dict):
                    raise LiveCheckError("Issues API item is not an object")
                if "pull_request" in value:
                    continue
                title = value.get("title")
                if (
                    value.get("state") != "open"
                    or not isinstance(title, str)
                    or len(title) > 512
                    or not title.startswith(REQUEST_TITLE_PREFIX)
                ):
                    continue
                result.append(
                    {
                        "created_at": value.get("created_at"),
                        "id": value.get("id"),
                        "number": value.get("number"),
                    }
                )
            if len(values) < PAGE_SIZE:
                return result
            if page == self.max_pages:
                raise LiveCheckError("Issues API pagination exceeded the page limit")
        return result

    def fetch_process_runs(self, cutoff: datetime) -> list[dict[str, Any]]:
        owner, name = self.config.repository.split("/", 1)
        prefix = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        result: list[dict[str, Any]] = []
        previous_created: datetime | None = None
        for page in range(1, self.max_pages + 1):
            query = urllib.parse.urlencode(
                {"branch": "main", "page": page, "per_page": PAGE_SIZE}
            )
            value = self._api(
                f"{prefix}/actions/workflows/process.yml/runs?{query}"
            )
            if not isinstance(value, dict) or not isinstance(
                value.get("workflow_runs"), list
            ):
                raise LiveCheckError("Actions API response has an invalid shape")
            runs = value["workflow_runs"]
            if len(runs) > PAGE_SIZE:
                raise LiveCheckError("Actions API page exceeds the item limit")
            crossed_cutoff = False
            for run in runs:
                normalized = _normalize_run(run)
                created = _parse_timestamp(
                    normalized["created_at"], "workflow run"
                )
                if previous_created is not None and created > previous_created:
                    raise LiveCheckError("Actions API runs are not newest-first")
                previous_created = created
                crossed_cutoff = crossed_cutoff or created <= cutoff
                result.append(normalized)
            if len(runs) < PAGE_SIZE or crossed_cutoff:
                return result
            if page == self.max_pages:
                raise LiveCheckError("Actions API pagination exceeded the page limit")
        return result

    def fetch_pages(self) -> Any:
        owner, name = self.config.repository.split("/", 1)
        owner = urllib.parse.quote(owner, safe="")
        name = urllib.parse.quote(name, safe="")
        return self._api(f"/repos/{owner}/{name}/pages")


def _safe_positive_int(value: Any, context: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > 9_007_199_254_740_991
    ):
        raise LiveCheckError(f"{context} is invalid")
    return value


def _normalize_run(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LiveCheckError("workflow run is not an object")
    run_id = _safe_positive_int(value.get("id"), "workflow run id")
    status = value.get("status")
    conclusion = value.get("conclusion")
    if not isinstance(status, str) or _RUN_VALUE_RE.fullmatch(status) is None:
        raise LiveCheckError("workflow run status is invalid")
    if conclusion is not None and (
        not isinstance(conclusion, str)
        or _RUN_VALUE_RE.fullmatch(conclusion) is None
    ):
        raise LiveCheckError("workflow run conclusion is invalid")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    _parse_timestamp(created_at, "workflow run created_at")
    _parse_timestamp(updated_at, "workflow run updated_at")
    return {
        "conclusion": conclusion,
        "created_at": created_at,
        "id": run_id,
        "status": status,
        "updated_at": updated_at,
    }


def collect_snapshot(
    client: GitHubLiveClient,
    *,
    now: datetime,
    max_process_age: timedelta,
) -> dict[str, Any]:
    def collect(label: str, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except LiveCheckError as exc:
            raise LiveCheckError(f"{label}: {exc}") from exc

    nonce = _format_timestamp(now).replace(":", "").replace("-", "")
    command_issues = collect("Issues API", client.fetch_open_command_issues)
    pages = collect("Pages API", client.fetch_pages)
    process_runs = collect(
        "Actions API",
        lambda: client.fetch_process_runs(now - max_process_age),
    )
    contents = collect("Contents API registry", client.fetch_contents_registry)
    pages_registry = collect(
        "Pages registry",
        lambda: client.fetch_public_registry(
            "pages", client.config.pages_base, nonce
        ),
    )
    raw_registry = collect(
        "raw registry",
        lambda: client.fetch_public_registry(
            "raw", client.config.raw_base, nonce
        ),
    )
    return {
        "command_issues": command_issues,
        "pages": pages,
        "process_runs": process_runs,
        "registries": {
            "contents": contents,
            "pages": pages_registry,
            "raw": raw_registry,
        },
    }


def _validate_registry(
    value: Any,
    *,
    source: str,
    repository: str,
    profile: str,
    raw_base: str,
    pages_base: str,
) -> tuple[str, bool]:
    if not isinstance(value, dict):
        raise LiveCheckError(f"{source} registry is not an object")
    if value.get("schema") != BASE_SCHEMA:
        raise LiveCheckError(f"{source} registry schema is invalid")
    if value.get("profile") != profile:
        raise LiveCheckError(f"{source} registry profile is invalid")
    generation = value.get("generation_sha256")
    if not isinstance(generation, str) or _SHA256_RE.fullmatch(generation) is None:
        raise LiveCheckError(f"{source} registry generation is invalid")
    expected_owner, expected_name = repository.split("/", 1)
    identity = value.get("repository")
    if (
        not isinstance(identity, dict)
        or identity.get("owner") != expected_owner
        or identity.get("name") != expected_name
        or identity.get("branch") != "main"
    ):
        raise LiveCheckError(f"{source} registry repository is invalid")
    if (
        not isinstance(value.get("raw_base"), str)
        or validate_raw_base(value["raw_base"], repository) != raw_base
    ):
        raise LiveCheckError(f"{source} registry raw base is invalid")
    if (
        not isinstance(value.get("pages_base"), str)
        or validate_pages_base(value["pages_base"], repository) != pages_base
    ):
        raise LiveCheckError(f"{source} registry Pages base is invalid")
    summary = value.get("summary")
    request_versions = value.get("immutable_request_versions")
    zero_counts = (
        isinstance(summary, dict)
        and all(
            type(summary.get(key)) is int and summary[key] == 0
            for key in ("events", "requests", "receipts")
        )
    )
    clean = (
        zero_counts and isinstance(request_versions, list) and not request_versions
    )
    return generation, clean


def _run_report(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "conclusion": value["conclusion"],
        "created_at": value["created_at"],
        "id": value["id"],
        "status": value["status"],
        "updated_at": value["updated_at"],
    }


def evaluate_snapshot(
    snapshot: Mapping[str, Any],
    config: LiveConfig,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Purely evaluate a bounded snapshot and return a safe JSON summary."""

    if now.tzinfo is None:
        raise LiveCheckError("evaluation time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if not isinstance(snapshot, Mapping):
        raise LiveCheckError("live snapshot is invalid")

    registries = snapshot.get("registries")
    if not isinstance(registries, Mapping) or set(registries) != {
        "contents",
        "pages",
        "raw",
    }:
        raise LiveCheckError("registry snapshot is incomplete")
    expected_urls = {
        "contents": contents_registry_url(config.repository),
        "pages": f"{config.pages_base.rstrip('/')}/registry.json",
        "raw": f"{config.raw_base}/registry.json",
    }
    generations: dict[str, str] = {}
    content_digests: dict[str, str] = {}
    clean_sources: list[bool] = []
    registry_report: dict[str, dict[str, str]] = {}
    for source in ("contents", "pages", "raw"):
        item = registries[source]
        if (
            not isinstance(item, Mapping)
            or item.get("url") != expected_urls[source]
        ):
            raise LiveCheckError(f"{source} registry URL is invalid")
        generation, clean = _validate_registry(
            item.get("document"),
            source=source,
            repository=config.repository,
            profile=config.profile,
            raw_base=config.raw_base,
            pages_base=config.pages_base,
        )
        generations[source] = generation
        content_digest = _canonical_json_digest(item.get("document"))
        content_digests[source] = content_digest
        clean_sources.append(clean)
        registry_report[source] = {
            "content_sha256": content_digest,
            "generation_sha256": generation,
            "url": expected_urls[source],
        }
    if len(set(generations.values())) != 1:
        detail = ", ".join(
            f"{source}={generations[source]}" for source in sorted(generations)
        )
        raise LiveCheckError(f"registry generations differ: {detail}")
    if len(set(content_digests.values())) != 1:
        detail = ", ".join(
            f"{source}={content_digests[source]}"
            for source in sorted(content_digests)
        )
        raise LiveCheckError(f"registry content digests differ: {detail}")

    issues = snapshot.get("command_issues")
    if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes)):
        raise LiveCheckError("command Issue snapshot is invalid")
    if len(issues) > MAX_PAGES * PAGE_SIZE:
        raise LiveCheckError("command Issue snapshot exceeds the item limit")
    normalized_issues: list[tuple[datetime, dict[str, Any]]] = []
    issue_ids: set[int] = set()
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise LiveCheckError("command Issue item is invalid")
        issue_id = _safe_positive_int(issue.get("id"), "Issue id")
        number = _safe_positive_int(issue.get("number"), "Issue number")
        if issue_id in issue_ids:
            raise LiveCheckError("command Issue snapshot contains duplicate ids")
        issue_ids.add(issue_id)
        created = _parse_timestamp(issue.get("created_at"), "Issue created_at")
        if created > now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
            raise LiveCheckError("command Issue timestamp is in the future")
        normalized_issues.append(
            (
                created,
                {
                    "created_at": issue.get("created_at"),
                    "id": issue_id,
                    "number": number,
                },
            )
        )
    normalized_issues.sort(key=lambda item: (item[0], item[1]["id"]))
    oldest_report: dict[str, Any] | None = None
    if normalized_issues:
        oldest_time, oldest = normalized_issues[0]
        oldest_age = max(0.0, (now - oldest_time).total_seconds())
        oldest_report = {**oldest, "age_seconds": int(oldest_age)}
        if oldest_age > config.max_command_age.total_seconds():
            raise LiveCheckError(
                f"oldest command Issue #{oldest['number']} exceeds the age limit"
            )

    runs = snapshot.get("process_runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
        raise LiveCheckError("process workflow snapshot is invalid")
    if len(runs) > MAX_PAGES * PAGE_SIZE:
        raise LiveCheckError("process workflow snapshot exceeds the item limit")
    normalized_runs: list[tuple[datetime, datetime, dict[str, Any]]] = []
    run_ids: set[int] = set()
    for raw_run in runs:
        run = _normalize_run(raw_run)
        if run["id"] in run_ids:
            raise LiveCheckError("process workflow snapshot contains duplicate ids")
        run_ids.add(run["id"])
        created = _parse_timestamp(run["created_at"], "workflow run created_at")
        updated = _parse_timestamp(run["updated_at"], "workflow run updated_at")
        if updated < created:
            raise LiveCheckError("workflow run update precedes its creation")
        if created > now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
            raise LiveCheckError("workflow run timestamp is in the future")
        if run["status"] == "completed" and run["conclusion"] is None:
            raise LiveCheckError("completed workflow run lacks a conclusion")
        normalized_runs.append((created, updated, run))

    process_report: dict[str, Any]
    if not normalized_runs:
        if not (
            config.allow_no_process_run
            and all(clean_sources)
            and not normalized_issues
        ):
            raise LiveCheckError("no process.yml workflow run exists")
        process_report = {
            "allow_no_process_run": True,
            "latest": None,
            "latest_completed": None,
            "latest_success": None,
        }
    else:
        latest = max(normalized_runs, key=lambda item: (item[0], item[2]["id"]))
        completed = [
            item for item in normalized_runs if item[2]["status"] == "completed"
        ]
        successes = [
            item for item in completed if item[2]["conclusion"] == "success"
        ]
        latest_completed = (
            max(completed, key=lambda item: (item[1], item[2]["id"]))
            if completed
            else None
        )
        latest_success = (
            max(successes, key=lambda item: (item[1], item[2]["id"]))
            if successes
            else None
        )
        if latest_completed is not None and latest_completed[2]["conclusion"] != "success":
            if (
                latest_success is None
                or latest_success[1] <= latest_completed[1]
            ):
                raise LiveCheckError(
                    "newest completed process.yml run "
                    f"{latest_completed[2]['id']} did not succeed"
                )
        if latest_success is None:
            raise LiveCheckError(
                "no successful process.yml run exists within the age limit"
            )
        success_age = max(0.0, (now - latest_success[1]).total_seconds())
        if success_age > config.max_process_age.total_seconds():
            raise LiveCheckError("latest successful process.yml run is stale")
        process_report = {
            "allow_no_process_run": False,
            "latest": _run_report(latest[2]),
            "latest_completed": (
                _run_report(latest_completed[2])
                if latest_completed is not None
                else None
            ),
            "latest_success": {
                **_run_report(latest_success[2]),
                "age_seconds": int(success_age),
            },
        }

    pages = snapshot.get("pages")
    if not isinstance(pages, Mapping):
        raise LiveCheckError("GitHub Pages is not configured")
    pages_status = pages.get("status")
    build_type = pages.get("build_type")
    if build_type is not None and (
        not isinstance(build_type, str)
        or _RUN_VALUE_RE.fullmatch(build_type) is None
    ):
        raise LiveCheckError("GitHub Pages build type is invalid")
    # Workflow-built Pages sites report a null API status; the already-validated
    # Pages registry is the corresponding published-build evidence.
    built = pages_status == "built" or (
        pages_status is None and build_type == "workflow"
    )
    if not built:
        raise LiveCheckError("GitHub Pages site is not built")
    html_url = pages.get("html_url")
    if (
        not isinstance(html_url, str)
        or validate_pages_base(html_url, config.repository) != config.pages_base
    ):
        raise LiveCheckError("GitHub Pages site URL is invalid")

    return {
        "backlog": {
            "count": len(normalized_issues),
            "oldest": oldest_report,
        },
        "checked_at": _format_timestamp(now),
        "pages": {
            "api_status": pages_status,
            "build_type": build_type,
            "status": "built",
            "url": config.pages_base,
        },
        "process": process_report,
        "registries": registry_report,
        "repository": config.repository,
        "schema": LIVE_SCHEMA,
        "thresholds": {
            "max_command_age_seconds": int(
                config.max_command_age.total_seconds()
            ),
            "max_process_age_seconds": int(
                config.max_process_age.total_seconds()
            ),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the hosted RAPP Base operational boundary."
    )
    parser.add_argument("--repository")
    parser.add_argument("--raw-base")
    parser.add_argument("--pages-base")
    parser.add_argument(
        "--max-command-age-minutes",
        type=_positive_number,
        default=30.0,
    )
    parser.add_argument(
        "--max-process-age-hours",
        type=_positive_number,
        default=12.0,
    )
    parser.add_argument(
        "--allow-no-process-run",
        action="store_true",
        help="allow no process run only when the published template is clean",
    )
    return parser


def config_from_args(
    args: argparse.Namespace,
    *,
    manifest: Mapping[str, Any],
    environ: Mapping[str, str],
) -> LiveConfig:
    manifest_repository = manifest["repository"]
    default_repository = (
        f"{manifest_repository['owner']}/{manifest_repository['name']}"
    )
    repository = validate_repository(
        args.repository
        or environ.get("GITHUB_REPOSITORY")
        or default_repository
    )
    raw_base = validate_raw_base(
        args.raw_base or default_raw_base(repository), repository
    )
    pages_base = validate_pages_base(
        args.pages_base or default_pages_base(repository), repository
    )
    token = environ.get("GITHUB_TOKEN", "")
    if (
        not token
        or len(token) > 4096
        or "\r" in token
        or "\n" in token
    ):
        raise LiveCheckError("GITHUB_TOKEN is required")
    return LiveConfig(
        allow_no_process_run=args.allow_no_process_run,
        max_command_age=timedelta(minutes=args.max_command_age_minutes),
        max_process_age=timedelta(hours=args.max_process_age_hours),
        pages_base=pages_base,
        profile=manifest["profile"],
        raw_base=raw_base,
        repository=repository,
        token=token,
    )


def run(config: LiveConfig, *, now: datetime | None = None) -> dict[str, Any]:
    checked_at = now or datetime.now(timezone.utc)
    pages_host = urllib.parse.urlsplit(config.pages_base).hostname
    if pages_host is None:
        raise LiveCheckError("Pages base is invalid")
    http = HTTPAdapter(config.token, pages_host)
    client = GitHubLiveClient(http, config)
    snapshot = collect_snapshot(
        client,
        now=checked_at,
        max_process_age=config.max_process_age,
    )
    return evaluate_snapshot(snapshot, config, now=checked_at)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(ROOT)
        config = config_from_args(args, manifest=manifest, environ=os.environ)
        summary = run(config)
    except RappError as exc:
        print(f"live check failed: {exc.message}", file=sys.stderr)
        return 1
    except LiveCheckError as exc:
        print(f"live check failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("live check failed: could not read local configuration", file=sys.stderr)
        return 1
    print(
        json.dumps(
            summary,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
