#!/usr/bin/env python3
"""Deliver only receipts already reachable from remote main."""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.errors import RappError
from rapp_base.github import GitHubClient
from rapp_base.constants import REQUEST_LABEL
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import load_receipts


def _remote_receipt_is_identical(
    client: GitHubClient, relative: str, local_bytes: bytes
) -> bool:
    encoded_path = urllib.parse.quote(relative, safe="/")
    value = client.request(
        "GET",
        f"/repos/{client.repository}/contents/{encoded_path}?ref=main",
    )
    if not isinstance(value, dict) or value.get("encoding") != "base64":
        return False
    try:
        remote = base64.b64decode(value["content"], validate=False)
    except (KeyError, ValueError, TypeError):
        return False
    return remote == local_bytes


def _comment_body(receipt: dict, repository: str, state_path: str) -> str:
    marker = f"<!-- rapp-base-receipt:{receipt['receipt_id']} -->"
    lines = [
        "### RAPP Base receipt",
        "",
        f"- Status: **{receipt['status']}**",
        f"- Code: `{receipt['code']}`",
        f"- Receipt: `{receipt['receipt_id']}`",
    ]
    if receipt["command_id"] is not None:
        lines.append(f"- Command: `{receipt['command_id']}`")
    if receipt["record"] is not None:
        lines.extend(
            [
                f"- Record: `{receipt['record']['collection']}/{receipt['record']['id']}`",
                f"- Revision: `{receipt['record']['revision']}`",
            ]
        )
    lines.extend(
        [
            "",
            receipt["message"],
            "",
            f"[Committed receipt](https://github.com/{repository}/blob/main/{state_path})",
            "",
            marker,
        ]
    )
    return "\n".join(lines)


def deliver_receipts(
    root: Path,
    manifest: dict,
    client: GitHubClient,
    *,
    trusted_bot: str = "github-actions[bot]",
) -> tuple[int, list[str]]:
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?(?:\[bot\])?", trusted_bot) is None:
        raise RappError("invalid_bot_identity", "trusted receipt bot login is invalid")
    receipts = load_receipts(root, manifest)
    pending = {
        issue["id"]: issue
        for issue in client.fetch_request_issues(
            limit=manifest["limits"]["issues_per_reconcile"]
        )
    }
    delivered = 0
    failures: list[str] = []
    for issue_id, receipt in sorted(receipts.items()):
        issue = pending.get(issue_id)
        if issue is None:
            continue
        number = receipt["issue"]["number"]
        state_path = f"state/receipts/issue-{issue_id}.json"
        try:
            local_bytes = (root / state_path).read_bytes()
            if not _remote_receipt_is_identical(client, state_path, local_bytes):
                raise RappError(
                    "receipt_not_reachable",
                    f"{state_path} is not yet reachable from remote main",
                )
            if (
                issue["id"] != receipt["issue"]["id"]
                or issue["node_id"] != receipt["issue"]["node_id"]
            ):
                raise RappError(
                    "issue_identity_changed",
                    f"issue #{number} no longer matches the admitted identity",
                )
            body = _comment_body(receipt, client.repository, state_path)
            comments = client.get_all(
                f"/repos/{client.repository}/issues/{number}/comments",
                limit=1000,
            )
            found = any(
                isinstance(comment, dict)
                and comment.get("body") == body
                and isinstance(comment.get("user"), dict)
                and comment["user"].get("login") == trusted_bot
                for comment in comments
            )
            if not found:
                client.request(
                    "POST",
                    f"/repos/{client.repository}/issues/{number}/comments",
                    {"body": body},
                )
            remaining_labels = [
                label for label in issue["labels"] if label != REQUEST_LABEL
            ]
            client.request(
                "PATCH",
                f"/repos/{client.repository}/issues/{number}",
                {"labels": remaining_labels, "state": "closed"},
            )
            delivered += 1
        except (OSError, RappError) as exc:
            message = exc.message if isinstance(exc, RappError) else str(exc)
            failures.append(f"Issue #{number}: {message}")
    return delivered, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        manifest = load_manifest(root)
        client = GitHubClient(
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_REPOSITORY", ""),
        )
        delivered, failures = deliver_receipts(
            root,
            manifest,
            client,
            trusted_bot=os.environ.get(
                "RAPP_BASE_TRUSTED_BOT_LOGIN",
                "github-actions[bot]",
            ),
        )
    except (OSError, RappError) as exc:
        message = exc.message if isinstance(exc, RappError) else str(exc)
        print(f"delivery failed: {message}", file=sys.stderr)
        return 1
    print(f"delivered or verified {delivered} receipt(s)")
    if failures:
        for failure in failures:
            print(f"delivery warning: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
