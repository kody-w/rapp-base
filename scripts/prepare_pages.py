#!/usr/bin/env python3
"""Assemble the allowlisted GitHub Pages artifact."""

from __future__ import annotations

import argparse
import os
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


def _reject_symlinks(source: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"Pages source is a symlink: {source}")
    if not source.is_dir():
        return
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            if path.is_symlink():
                raise ValueError(f"Pages source contains a symlink: {path}")


def prepare(output: Path, root: Path = ROOT) -> None:
    root = root.resolve()
    if output.is_symlink():
        raise ValueError("Pages output cannot be a symlink")
    output = output.resolve()
    if output == root or root not in output.parents:
        raise ValueError("Pages output must be a child of the repository")
    for relative in FILES:
        source = root / relative
        _reject_symlinks(source)
        if not source.is_file():
            raise FileNotFoundError(source)
    for relative in DIRECTORIES:
        source = root / relative
        _reject_symlinks(source)
        if not source.is_dir():
            raise FileNotFoundError(source)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for relative in FILES:
        source = root / relative
        shutil.copy2(source, output / relative)
    for relative in DIRECTORIES:
        source = root / relative
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
