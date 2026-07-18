#!/usr/bin/env python3
"""Build all deterministic static projections."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.build import build
from rapp_base.errors import RappError
from rapp_base.manifest import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--check", action="store_true", help="verify outputs without writing"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        summary = build(root, load_manifest(root), write=not args.check)
    except RappError as exc:
        print(f"build failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

