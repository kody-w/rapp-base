#!/usr/bin/env python3
"""Assemble the allowlisted GitHub Pages artifact."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FILES = (
    ".nojekyll",
    "index.html",
    "explorer.js",
    "styles.css",
    "llms.txt",
    "registry.json",
    "README.md",
    "SPEC.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "LICENSE",
)
DIRECTORIES = ("api", "versions", "sdk", "schemas", ".well-known")


def prepare(output: Path) -> None:
    output = output.resolve()
    if output == ROOT or ROOT not in output.parents:
        raise ValueError("Pages output must be a child of the repository")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for relative in FILES:
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, output / relative)
    for relative in DIRECTORIES:
        source = ROOT / relative
        if not source.is_dir():
            raise FileNotFoundError(source)
        shutil.copytree(source, output / relative)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare(args.output)
    except (OSError, ValueError) as exc:
        print(f"prepare-pages failed: {exc}", file=sys.stderr)
        return 1
    print(f"prepared {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
