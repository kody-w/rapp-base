#!/usr/bin/env python3
"""Build an isolated synthetic ledger and report repository growth."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.build import build
from rapp_base.errors import RappError
from rapp_base.jsonutil import canonical_bytes, render_issue_form_body
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import load_requests, reconcile_document
from rapp_base.state import head_for_events, replay
from scripts.prepare_pages import DIRECTORIES, FILES

_BASELINE_EXCLUDES = {
    ".git",
    ".pages",
    ".scale-work",
    ".test-work",
    ".work",
    "api",
    "registry.json",
    "state",
    "versions",
}


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("counts must be non-negative")
    return parsed


def _command_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


def _issue(number: int, command: dict[str, Any] | str) -> dict[str, Any]:
    text = (
        command
        if isinstance(command, str)
        else json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    )
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=number)
    timestamp = instant.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "author_association": "OWNER",
        "body": render_issue_form_body(text),
        "created_at": timestamp,
        "id": 8_000_000_000 + number,
        "labels": [],
        "node_id": f"I_scale{number}",
        "number": number,
        "state": "open",
        "title": "[RAPP Base] scale probe",
        "updated_at": timestamp,
        "user": {"id": 42},
    }


def _repository(manifest: dict[str, Any]) -> dict[str, Any]:
    configured = manifest["repository"]
    return {
        "full_name": f"{configured['owner']}/{configured['name']}",
        "id": 9_000_000_001,
        "node_id": "R_scale_probe",
    }


def _reconcile_batches(
    root: Path,
    manifest: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    limit = manifest["limits"]["issues_per_reconcile"]
    repository = _repository(manifest)
    for offset in range(0, len(issues), limit):
        reconcile_document(
            root,
            manifest,
            {"issues": issues[offset : offset + limit], "repository": repository},
        )


def _measure(stages: dict[str, float], name: str, operation: Callable[[], None]) -> None:
    started = time.perf_counter()
    operation()
    stages[name] = round(time.perf_counter() - started, 6)


def _tree_metrics(root: Path) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    sizes = {path: path.stat().st_size for path in files}
    largest_files = [
        {"bytes": size, "path": path.relative_to(root).as_posix()}
        for path, size in sorted(
            sizes.items(), key=lambda item: (-item[1], item[0].as_posix())
        )[:10]
    ]
    directory_sizes: dict[Path, int] = {}
    for path, size in sizes.items():
        parent = path.parent
        while root in (parent, *parent.parents):
            directory_sizes[parent] = directory_sizes.get(parent, 0) + size
            if parent == root:
                break
            parent = parent.parent
    largest_directories = [
        {
            "bytes": size,
            "path": "." if path == root else path.relative_to(root).as_posix(),
        }
        for path, size in sorted(
            directory_sizes.items(),
            key=lambda item: (-item[1], item[0].as_posix()),
        )[:10]
    ]
    return len(files), sum(sizes.values()), largest_files, largest_directories


def _copy_baseline(source_root: Path, synthetic_root: Path) -> None:
    synthetic_root.mkdir(parents=True)
    for source in source_root.iterdir():
        if source.name in _BASELINE_EXCLUDES:
            continue
        if source.is_symlink():
            raise RappError("scale_probe", f"baseline contains a symlink: {source.name}")
        target = synthetic_root / source.name
        if source.is_dir():
            for current, directories, files in os.walk(source, followlinks=False):
                current_path = Path(current)
                for name in [*directories, *files]:
                    if (current_path / name).is_symlink():
                        relative = (current_path / name).relative_to(source_root)
                        raise RappError(
                            "scale_probe",
                            f"baseline contains a symlink: {relative}",
                        )
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def _pages_bytes(synthetic_root: Path) -> int:
    total = 0
    for relative in FILES:
        source = synthetic_root / relative
        total += source.stat().st_size
    for relative in DIRECTORIES:
        source = synthetic_root / relative
        total += sum(
            path.stat().st_size for path in source.rglob("*") if path.is_file()
        )
    return total


def run_probe(
    source_root: Path,
    *,
    creates: int,
    updates: int,
    deletes: int,
    rejections: int,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    scratch_parent = source_root / ".scale-work"
    scratch = scratch_parent / f"probe-{uuid.uuid4().hex}"
    started = time.perf_counter()
    stages: dict[str, float] = {}
    try:
        setup_started = time.perf_counter()
        _copy_baseline(source_root, scratch)
        for relative in ("state/events", "state/requests", "state/receipts"):
            (scratch / relative).mkdir(parents=True, exist_ok=True)
        manifest = load_manifest(scratch)
        (scratch / "state/head.json").write_bytes(
            canonical_bytes(head_for_events(manifest, []))
        )
        stages["setup"] = round(time.perf_counter() - setup_started, 6)
        issue_number = 1

        create_issues: list[dict[str, Any]] = []
        for index in range(creates):
            command = {
                "schema": "rapp-base-command/1.0",
                "collection": "resources",
                "command_id": _command_id(issue_number),
                "data": {
                    "free": True,
                    "kind": "article",
                    "rating": 4,
                    "summary": f"Synthetic scale probe resource {index}.",
                    "title": f"Scale probe resource {index}",
                    "topics": ["scale", f"batch-{index // 100}"],
                    "url": f"https://example.com/scale/{index}",
                },
                "operation": "create",
            }
            create_issues.append(_issue(issue_number, command))
            issue_number += 1
        _measure(
            stages,
            "create_admission",
            lambda: _reconcile_batches(scratch, manifest, create_issues),
        )

        def apply_updates() -> None:
            nonlocal issue_number
            remaining = updates
            round_number = 0
            while remaining:
                state = replay(scratch, manifest)
                records = [
                    record
                    for record in state.records["resources"].values()
                    if not record["deleted"]
                ]
                if not records:
                    raise RappError("scale_probe", "updates require an active record")
                batch: list[dict[str, Any]] = []
                for record in sorted(records, key=lambda item: item["id"])[:remaining]:
                    command = {
                        "schema": "rapp-base-command/1.0",
                        "collection": "resources",
                        "command_id": _command_id(issue_number),
                        "data": {
                            "summary": (
                                f"Synthetic update {round_number} for {record['id']}."
                            )
                        },
                        "if_revision": record["revision"],
                        "operation": "update",
                        "record_id": record["id"],
                    }
                    batch.append(_issue(issue_number, command))
                    issue_number += 1
                _reconcile_batches(scratch, manifest, batch)
                remaining -= len(batch)
                round_number += 1

        _measure(stages, "update_admission", apply_updates)

        def apply_deletes() -> None:
            nonlocal issue_number
            state = replay(scratch, manifest)
            records = sorted(
                (
                    record
                    for record in state.records["resources"].values()
                    if not record["deleted"]
                ),
                key=lambda item: item["id"],
            )
            if deletes > len(records):
                raise RappError(
                    "scale_probe", "deletes exceed the available active records"
                )
            issues: list[dict[str, Any]] = []
            for record in records[:deletes]:
                command = {
                    "schema": "rapp-base-command/1.0",
                    "collection": "resources",
                    "command_id": _command_id(issue_number),
                    "if_revision": record["revision"],
                    "operation": "delete",
                    "record_id": record["id"],
                }
                issues.append(_issue(issue_number, command))
                issue_number += 1
            _reconcile_batches(scratch, manifest, issues)

        _measure(stages, "delete_admission", apply_deletes)

        rejection_issues: list[dict[str, Any]] = []
        for index in range(rejections):
            candidate = (
                '{"schema":"rapp-base-command/1.0",'
                f'"command_id":"{_command_id(issue_number)}",'
                '"operation":"create","collection":"resources",'
                f'"probe":"rejected-{index}","probe":"duplicate-{index}"}}'
            )
            rejection_issues.append(_issue(issue_number, candidate))
            issue_number += 1
        _measure(
            stages,
            "rejection_admission",
            lambda: _reconcile_batches(scratch, manifest, rejection_issues),
        )
        _measure(stages, "projection_build", lambda: build(scratch, manifest))

        state = replay(scratch, manifest)
        requests = load_requests(scratch, manifest)
        per_collection: dict[str, dict[str, int]] = {}
        active = 0
        tombstones = 0
        for name, records in sorted(state.records.items()):
            collection_active = sum(not record["deleted"] for record in records.values())
            collection_tombstones = len(records) - collection_active
            active += collection_active
            tombstones += collection_tombstones
            per_collection[name] = {
                "active": collection_active,
                "lifetime": len(records),
                "remaining_active_slots": max(
                    0,
                    manifest["limits"]["records_per_collection"] - collection_active,
                ),
                "tombstones": collection_tombstones,
            }
        file_count, byte_count, largest_files, largest_directories = _tree_metrics(
            scratch
        )
        elapsed = round(time.perf_counter() - started, 6)
        stages["total"] = elapsed
        event_remaining = max(
            0, manifest["limits"]["events"] - len(state.events)
        )
        request_remaining = max(
            0, manifest["limits"]["requests"] - len(requests)
        )
        return {
            "active": active,
            "bytes": byte_count,
            "elapsed_seconds": stages,
            "events": len(state.events),
            "files": file_count,
            "largest_directories": largest_directories,
            "largest_files": largest_files,
            "pages_artifact_bytes_estimate": _pages_bytes(scratch),
            "per_collection": per_collection,
            "projected_headroom": {
                "active_slots": sum(
                    value["remaining_active_slots"]
                    for value in per_collection.values()
                ),
                "events": event_remaining,
                "requests": request_remaining,
                "request_event_minimum": min(event_remaining, request_remaining),
            },
            "requests": len(requests),
            "scenario": {
                "creates": creates,
                "deletes": deletes,
                "rejections": rejections,
                "updates": updates,
            },
            "tombstones": tombstones,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            scratch_parent.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--creates", type=_nonnegative, default=25)
    parser.add_argument("--updates", type=_nonnegative, default=25)
    parser.add_argument("--deletes", type=_nonnegative, default=5)
    parser.add_argument("--rejections", type=_nonnegative, default=5)
    args = parser.parse_args()
    try:
        result = run_probe(
            args.root,
            creates=args.creates,
            updates=args.updates,
            deletes=args.deletes,
            rejections=args.rejections,
        )
    except (OSError, RappError, ValueError) as exc:
        print(f"scale probe failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
