#!/usr/bin/env python3
"""Fetch and normalize open GitHub Issues with the fixed RAPP Base title prefix."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.errors import RappError
from rapp_base.github import GitHubClient
from rapp_base.jsonutil import transport_bytes, write_bytes_atomic
from rapp_base.manifest import load_manifest

EVENT_BYTES = 16 * 1024 * 1024


def load_event(path: Path) -> object:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RappError("github_event", "cannot read the GitHub event payload") from exc
    if len(data) > EVENT_BYTES:
        raise RappError("github_event", "GitHub event payload exceeds the byte limit")
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RappError("github_event", "GitHub event payload is invalid JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--event",
        type=Path,
        help="trusted GitHub event payload; only issues:opened is directly observed",
    )
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.root.resolve())
        client = GitHubClient(
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_REPOSITORY", ""),
        )
        event = load_event(args.event.resolve()) if args.event is not None else None
        document = client.fetch_reconciliation_document(
            limit=manifest["limits"]["issues_per_reconcile"],
            event=event,
        )
        write_bytes_atomic(args.output.resolve(), transport_bytes(document))
    except RappError as exc:
        print(f"fetch failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"issues": len(document["issues"])},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
