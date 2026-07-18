#!/usr/bin/env python3
"""Reconcile normalized GitHub Issue fixtures into canonical state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.errors import RappError
from rapp_base.constants import HARD_LIMITS
from rapp_base.jsonutil import load_json_file
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import reconcile_document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        manifest = load_manifest(root)
        document = load_json_file(
            args.input.resolve(),
            {
                **HARD_LIMITS,
                "array_items": max(
                    HARD_LIMITS["array_items"],
                    manifest["limits"]["issues_per_reconcile"],
                ),
                "json_nodes": manifest["limits"]["issues_per_reconcile"]
                * (HARD_LIMITS["json_nodes"] + 20),
                "object_keys": manifest["limits"]["issues_per_reconcile"]
                * (HARD_LIMITS["object_keys"] + 15),
                # GitHub limits Issue bodies by characters. Allow the largest
                # possible UTF-8 representation through the transport layer;
                # admission applies RAPP Base's stricter byte limit and emits
                # a durable rejection.
                "string_bytes": HARD_LIMITS["issue_body_bytes"] * 4,
            },
            byte_limit=64 * 1024 * 1024,
            allow_control_characters=True,
        )
        summary = reconcile_document(root, manifest, document)
    except RappError as exc:
        print(f"reconcile failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
