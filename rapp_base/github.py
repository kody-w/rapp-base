"""The only network adapter: narrowly scoped GitHub REST access."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .constants import REQUEST_TITLE_PREFIX
from .errors import RappError

API_BASE = "https://api.github.com"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class GitHubClient:
    def __init__(self, token: str, repository: str):
        if not token:
            raise RappError("missing_token", "GITHUB_TOKEN is required")
        if _REPOSITORY_RE.fullmatch(repository) is None:
            raise RappError("invalid_repository", "GITHUB_REPOSITORY must be owner/name")
        if any(part in {".", ".."} for part in repository.split("/")):
            raise RappError("invalid_repository", "GITHUB_REPOSITORY is unsafe")
        self._token = token
        self.repository = repository

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/") or "://" in path or "\r" in path or "\n" in path:
            raise RappError("unsafe_api_path", "GitHub API path is unsafe")
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "rapp-base/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            data = json.dumps(
                payload, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            API_BASE + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RappError(
                "github_api",
                f"GitHub API returned HTTP {exc.code}: {detail[:500]}",
            ) from exc
        except urllib.error.URLError as exc:
            raise RappError("github_api", f"GitHub API request failed: {exc.reason}") from exc
        if len(raw) > 16 * 1024 * 1024:
            raise RappError("github_api", "GitHub API response exceeded the byte limit")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RappError("github_api", "GitHub API returned invalid JSON") from exc

    def get_all(
        self, path: str, *, limit: int, truncate: bool = False
    ) -> list[Any]:
        separator = "&" if "?" in path else "?"
        items: list[Any] = []
        page = 1
        while True:
            response = self.request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if not isinstance(response, list):
                raise RappError("github_api", "expected a GitHub API array")
            if len(items) + len(response) > limit:
                if truncate:
                    items.extend(response[: limit - len(items)])
                    break
                raise RappError("github_api_limit", "GitHub API result exceeds local limit")
            items.extend(response)
            if len(response) < 100:
                break
            if truncate and len(items) == limit:
                break
            page += 1
        return items

    def fetch_repository(self) -> dict[str, Any]:
        value = self.request("GET", f"/repos/{self.repository}")
        return normalize_api_repository(value)

    def fetch_request_issues(self, *, limit: int) -> list[dict[str, Any]]:
        query = urllib.parse.quote(
            f'repo:{self.repository} is:issue is:open in:title "{REQUEST_TITLE_PREFIX}"',
            safe="",
        )
        result: list[dict[str, Any]] = []
        page = 1
        scanned = 0
        scan_limit = min(1000, max(100, limit * 4))
        while len(result) < limit and scanned < scan_limit:
            response = self.request(
                "GET",
                f"/search/issues?q={query}&sort=created&order=asc"
                f"&per_page=100&page={page}",
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("items"), list
            ):
                raise RappError("github_api", "expected a GitHub Search result")
            values = response["items"]
            scanned += len(values)
            for value in values:
                if isinstance(value, dict) and "pull_request" not in value:
                    issue = normalize_api_issue(value)
                    if (
                        issue["state"] == "open"
                        and issue["title"].startswith(REQUEST_TITLE_PREFIX)
                    ):
                        result.append(issue)
                    if len(result) == limit:
                        break
            if len(values) < 100:
                break
            page += 1
        if scanned >= scan_limit and len(result) < limit:
            raise RappError(
                "github_api_limit",
                "GitHub Search returned too many non-routable matches",
            )
        return result


def normalize_api_repository(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RappError("github_api", "repository response is invalid")
    try:
        return {
            "full_name": value["full_name"],
            "id": value["id"],
            "node_id": value["node_id"],
        }
    except KeyError as exc:
        raise RappError("github_api", "repository identity is incomplete") from exc


def normalize_api_issue(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RappError("github_api", "issue response is invalid")
    try:
        labels = [
            label["name"]
            for label in value["labels"]
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ]
        return {
            "author_association": value["author_association"],
            "body": value.get("body") or "",
            "created_at": value["created_at"],
            "id": value["id"],
            "labels": labels,
            "node_id": value["node_id"],
            "number": value["number"],
            "state": value["state"],
            "title": value["title"],
            "updated_at": value["updated_at"],
            "user": {"id": value["user"]["id"]},
        }
    except (KeyError, TypeError) as exc:
        raise RappError("github_api", "issue identity is incomplete") from exc
