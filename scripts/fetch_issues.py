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
from rapp_base.jsonutil import canonical_bytes, write_bytes_atomic
from rapp_base.manifest import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.root.resolve())
        client = GitHubClient(
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_REPOSITORY", ""),
        )
        document = {
            "issues": client.fetch_request_issues(
                limit=manifest["limits"]["issues_per_reconcile"]
            ),
            "repository": client.fetch_repository(),
        }
        write_bytes_atomic(args.output.resolve(), canonical_bytes(document))
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
