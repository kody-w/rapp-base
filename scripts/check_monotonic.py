#!/usr/bin/env python3
"""Reject non-append-only state relative to a Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_ROOTS = ("state/events", "state/requests", "state/receipts")
_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class MonotonicError(ValueError):
    pass


def _git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise MonotonicError(detail or f"git {' '.join(arguments)} failed")
    return result.stdout


def _commit(root: Path, reference: str) -> str:
    value = _git(
        root, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"
    ).decode("ascii", errors="strict").strip()
    if _OBJECT_ID_RE.fullmatch(value) is None:
        raise MonotonicError("base did not resolve to a full commit object")
    return value


def _tree_objects(root: Path, commit: str) -> dict[str, str]:
    raw = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        *IMMUTABLE_ROOTS,
        "state/head.json",
        "versions/index.json",
    )
    result: dict[str, str] = {}
    for record in raw.decode("utf-8", errors="strict").split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        parts = metadata.split()
        if (
            len(parts) != 3
            or parts[1] != "blob"
            or _OBJECT_ID_RE.fullmatch(parts[2]) is None
        ):
            raise MonotonicError(f"base path is not a blob: {path}")
        result[path] = parts[2]
    return result


def _blobs(root: Path, objects: dict[str, str]) -> dict[str, bytes]:
    paths = sorted(objects)
    if not paths:
        return {}
    result: dict[str, bytes] = {}
    chunk_size = 128
    for offset in range(0, len(paths), chunk_size):
        chunk = paths[offset : offset + chunk_size]
        request = b"".join(f"{objects[path]}\n".encode("ascii") for path in chunk)
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch"],
            input=request,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise MonotonicError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or "git cat-file --batch failed"
            )
        output = completed.stdout
        cursor = 0
        for path in chunk:
            line_end = output.find(b"\n", cursor)
            if line_end < 0:
                raise MonotonicError(f"could not read base blob header: {path}")
            header = output[cursor:line_end].decode("ascii", errors="strict")
            parts = header.split()
            if len(parts) != 3 or parts[1] != "blob":
                raise MonotonicError(f"base object is not a blob: {path}")
            size = int(parts[2])
            start = line_end + 1
            end = start + size
            if end >= len(output) or output[end : end + 1] != b"\n":
                raise MonotonicError(f"could not read base blob: {path}")
            result[path] = output[start:end]
            cursor = end + 1
        if cursor != len(output):
            raise MonotonicError("git cat-file returned unexpected trailing output")
    return result


def _json(data: bytes, context: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise MonotonicError(f"{context} contains duplicate key {key}")
            value[key] = item
        return value

    try:
        value = json.loads(data, object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MonotonicError(f"{context} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise MonotonicError(f"{context} must be a JSON object")
    return value


def _current_bytes(root: Path, relative: str) -> bytes:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in relative
        or not pure.parts
    ):
        raise MonotonicError(f"unsafe repository path: {relative}")
    path = root
    for part in pure.parts:
        path /= part
        if path.is_symlink():
            raise MonotonicError(f"current path traverses a symlink: {relative}")
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise MonotonicError(f"prior path was removed: {relative}") from exc
    except OSError as exc:
        raise MonotonicError(f"cannot read current path: {relative}") from exc


def _entries(index: dict[str, Any], context: str) -> dict[str, dict[str, Any]]:
    values = index.get("entries")
    if not isinstance(values, list):
        raise MonotonicError(f"{context} entries must be an array")
    result: dict[str, dict[str, Any]] = {}
    for entry in values:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise MonotonicError(f"{context} contains an invalid entry")
        path = entry["path"]
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in path
            or not path.startswith("versions/")
            or path == "versions/index.json"
        ):
            raise MonotonicError(f"{context} contains unsafe path {path}")
        if path in result:
            raise MonotonicError(f"{context} contains duplicate path {path}")
        result[path] = entry
    return result


def check_monotonic(root: Path, base: str) -> dict[str, int]:
    root = root.resolve()
    commit = _commit(root, base)
    objects = _tree_objects(root, commit)
    for required in ("state/head.json", "versions/index.json"):
        if required not in objects:
            raise MonotonicError(f"base commit lacks {required}")
    state_paths = sorted(
        path
        for path in objects
        if path.endswith(".json")
        and any(path.startswith(f"{prefix}/") for prefix in IMMUTABLE_ROOTS)
    )
    blobs = _blobs(
        root,
        {
            path: objects[path]
            for path in [*state_paths, "state/head.json", "versions/index.json"]
        },
    )

    for relative in state_paths:
        if _current_bytes(root, relative) != blobs[relative]:
            raise MonotonicError(f"prior immutable file changed: {relative}")

    base_head = _json(blobs["state/head.json"], "base head")
    current_head = _json(_current_bytes(root, "state/head.json"), "current head")
    if current_head.get("genesis_sha256") != base_head.get("genesis_sha256"):
        raise MonotonicError("state genesis changed")
    base_sequence = base_head.get("sequence")
    current_sequence = current_head.get("sequence")
    if (
        isinstance(base_sequence, bool)
        or not isinstance(base_sequence, int)
        or isinstance(current_sequence, bool)
        or not isinstance(current_sequence, int)
    ):
        raise MonotonicError("head sequence is invalid")
    if current_sequence < base_sequence:
        raise MonotonicError("head sequence decreased")
    if current_sequence == base_sequence:
        for field in ("event_hash", "event_path"):
            if current_head.get(field) != base_head.get(field):
                raise MonotonicError(f"head {field} changed without an appended event")

    base_index = _json(blobs["versions/index.json"], "base version index")
    current_index = _json(
        _current_bytes(root, "versions/index.json"), "current version index"
    )
    base_entries = _entries(base_index, "base version index")
    current_entries = _entries(current_index, "current version index")
    for path, entry in base_entries.items():
        if current_entries.get(path) != entry:
            raise MonotonicError(f"prior version index entry changed or disappeared: {path}")
        expected_hash = entry.get("content_sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise MonotonicError(f"base version entry has invalid content hash: {path}")
        if hashlib.sha256(_current_bytes(root, path)).hexdigest() != expected_hash:
            raise MonotonicError(f"prior indexed version content changed: {path}")

    return {
        "base_sequence": base_sequence,
        "current_sequence": current_sequence,
        "immutable_files": len(state_paths),
        "version_entries": len(base_entries),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Git commit/ref to compare")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        summary = check_monotonic(args.root, args.base)
    except (MonotonicError, OSError, UnicodeError, ValueError) as exc:
        print(f"monotonic check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
