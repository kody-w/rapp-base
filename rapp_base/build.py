"""Deterministic static API projection builder."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .constants import (
    API_PREFIX,
    BASE_SCHEMA,
    BUILDER_PROFILE,
    COMMAND_SCHEMA,
    HARD_LIMITS,
    PROFILE,
    REQUEST_TITLE_PREFIX,
)
from .errors import RappError
from .jsonutil import (
    canonical_bytes,
    expect_keys,
    load_json_file,
    object_hash,
    require_hash,
    sha256_bytes,
    timestamp_instant,
    write_bytes_atomic,
    write_bytes_immutable,
    write_json_atomic,
)
from .manifest import collection_map
from .reconcile import load_receipts, load_requests, verify_history
from .state import ReplayState, replay


def build(root: Path, manifest: dict[str, Any], *, write: bool = True) -> dict[str, Any]:
    state = replay(root, manifest, repair_head=write)
    requests = load_requests(root, manifest)
    receipts = load_receipts(root, manifest)
    verify_history(manifest, requests, receipts, state)
    head_changed = 0
    if write:
        head_changed = int(write_json_atomic(root / "state" / "head.json", state.head))
    generation = _generation_hash(manifest, state, requests, receipts)
    generated_at = _semantic_timestamp(manifest, state, requests)
    raw_base = _raw_base(manifest)
    pages_base = _pages_base(manifest)

    mutable: dict[str, bytes] = {}
    immutable: dict[str, tuple[bytes, str, str]] = {}
    collection_entries: list[dict[str, Any]] = []
    collections = collection_map(manifest)

    for name in sorted(collections):
        collection = collections[name]
        records = state.records[name]
        active = [
            records[record_id]
            for record_id in sorted(records)
            if not records[record_id]["deleted"]
        ]
        if len(active) > manifest["limits"]["snapshot_items"]:
            raise RappError(
                "snapshot_limit",
                f"collection {name} exceeds the bounded snapshot limit",
            )
        list_document = {
            "schema": "rapp-base-record-list/1.0",
            "items": active,
            "page": 1,
            "perPage": manifest["limits"]["snapshot_items"],
            "profile": PROFILE,
            "snapshot": {
                "bounded": True,
                "complete": True,
                "limit": manifest["limits"]["snapshot_items"],
            },
            "totalItems": len(active),
            "totalPages": math.ceil(
                len(active) / manifest["limits"]["snapshot_items"]
            ),
        }
        list_bytes = canonical_bytes(list_document)
        if len(list_bytes) > manifest["limits"]["generated_collection_bytes"]:
            raise RappError(
                "generated_bytes",
                f"collection {name} projection exceeds its byte limit",
            )
        records_path = f"{API_PREFIX}/collections/{name}/records.json"
        mutable[records_path] = list_bytes
        snapshot_sha = sha256_bytes(list_bytes)
        snapshot_path = f"versions/collections/{name}/{snapshot_sha[:12]}.json"
        _queue_immutable(
            immutable, snapshot_path, list_bytes, "collection", snapshot_sha
        )

        for record_id in sorted(records):
            record = records[record_id]
            mutable[
                f"{API_PREFIX}/collections/{name}/records/{record_id}.json"
            ] = canonical_bytes(record)
        for (history_collection, record_id), versions in sorted(state.history.items()):
            if history_collection != name:
                continue
            for record in versions:
                record_bytes = canonical_bytes(record)
                content_hash = sha256_bytes(record_bytes)
                version_path = (
                    f"versions/records/{name}/{record_id}/"
                    f"{content_hash[:12]}.json"
                )
                _queue_immutable(
                    immutable,
                    version_path,
                    record_bytes,
                    "record",
                    record["revision"],
                )

        collection_entries.append(
            {
                "count": len(active),
                "description": collection["description"],
                "fields": collection["fields"],
                "immutable_url": f"{raw_base}/{snapshot_path}",
                "name": name,
                "policies": collection["policies"],
                "records_url": f"{raw_base}/{records_path}",
                "record_url_template": (
                    f"{raw_base}/{API_PREFIX}/collections/{name}/records/{{id}}.json"
                ),
                "snapshot_sha256": snapshot_sha,
                "version_url_template": (
                    f"{raw_base}/versions/records/{name}/{{id}}/{{sha8}}.json"
                ),
            }
        )

    request_entries: list[dict[str, Any]] = []
    for request in sorted(requests.values(), key=_request_sort_key):
        data = canonical_bytes(request)
        content_hash = sha256_bytes(data)
        path = f"versions/requests/{content_hash[:12]}.json"
        _queue_immutable(
            immutable, path, data, "request", request["request_hash"]
        )
        request_entries.append(
            {
                "issue_id": request["issue"]["id"],
                "path": path,
                "request_hash": request["request_hash"],
            }
        )

    receipt_items: list[dict[str, Any]] = []
    issue_numbers: set[int] = set()
    command_paths: set[str] = set()
    for request in sorted(requests.values(), key=_request_sort_key):
        receipt = receipts.get(request["issue"]["id"])
        if receipt is None:
            continue
        issue_number = receipt["issue"]["number"]
        if issue_number in issue_numbers:
            raise RappError("receipt_identity", "duplicate issue number in receipts")
        issue_numbers.add(issue_number)
        issue_path = f"{API_PREFIX}/receipts/issue-{issue_number}.json"
        mutable[issue_path] = canonical_bytes(receipt)
        command_path = None
        if receipt["command_id"] is not None:
            candidate = f"{API_PREFIX}/receipts/commands/{receipt['command_id']}.json"
            if candidate not in command_paths:
                mutable[candidate] = canonical_bytes(receipt)
                command_paths.add(candidate)
                command_path = candidate
        receipt_items.append(
            {
                "code": receipt["code"],
                "command_id": receipt["command_id"],
                "command_url": (
                    f"{raw_base}/{command_path}" if command_path is not None else None
                ),
                "issue_number": issue_number,
                "receipt_id": receipt["receipt_id"],
                "status": receipt["status"],
                "url": f"{raw_base}/{issue_path}",
            }
        )
    mutable[f"{API_PREFIX}/receipts/index.json"] = canonical_bytes(
        {
            "schema": "rapp-base-receipt-index/1.0",
            "generation_sha256": generation,
            "items": receipt_items,
            "totalItems": len(receipt_items),
        }
    )

    event_items: list[dict[str, Any]] = []
    for event in state.events:
        event_path = f"{API_PREFIX}/events/{event['sequence']:08d}.json"
        mutable[event_path] = canonical_bytes(event)
        event_items.append(
            {
                "actor_id": event["actor"]["id"],
                "collection": event["collection"],
                "command_id": event["command_id"],
                "hash": event["hash"],
                "occurred_at": event["occurred_at"],
                "operation": event["operation"],
                "record_id": event["record_id"],
                "result_revision": event["result_revision"],
                "sequence": event["sequence"],
                "url": f"{raw_base}/{event_path}",
            }
        )
    mutable[f"{API_PREFIX}/events/index.json"] = canonical_bytes(
        {
            "schema": "rapp-base-event-index/1.0",
            "generation_sha256": generation,
            "head": state.head,
            "items": event_items,
            "totalItems": len(event_items),
        }
    )

    mutable[f"{API_PREFIX}/collections.json"] = canonical_bytes(
        {
            "schema": "rapp-base-collections/1.0",
            "generation_sha256": generation,
            "items": collection_entries,
            "profile": PROFILE,
            "totalItems": len(collection_entries),
        }
    )
    mutable[f"{API_PREFIX}/status.json"] = canonical_bytes(
        {
            "schema": "rapp-base-status/1.0",
            "collections": len(collection_entries),
            "events": len(state.events),
            "generator": BUILDER_PROFILE,
            "generated_at": generated_at,
            "generation_sha256": generation,
            "head": state.head,
            "healthy": True,
            "profile": PROFILE,
            "receipts": len(receipts),
            "requests": len(requests),
        }
    )
    status_url = f"{raw_base}/{API_PREFIX}/status.json"
    collections_url = f"{raw_base}/{API_PREFIX}/collections.json"
    events_url = f"{raw_base}/{API_PREFIX}/events/index.json"
    receipts_url = f"{raw_base}/{API_PREFIX}/receipts/index.json"
    versions_url = f"{raw_base}/versions/index.json"
    registry = {
        "schema": BASE_SCHEMA,
        "capabilities": {
            "consistency": "eventual-after-committed-workflow",
            "deletion": "audit-preserving-tombstone; Git history remains public",
            "limits": manifest["limits"],
            "read": {
                "cors": True,
                "snapshot": "one-complete-bounded-page",
                "transport": "static-json",
            },
            "unsupported": [
                "custom authentication",
                "private or personal data",
                "files or uploads",
                "hard deletion",
                "arbitrary code or hooks",
                "outbound URL fetching",
                "server-side realtime",
                "low-latency writes",
            ],
            "write": {
                "authentication": "GitHub issue author numeric user id",
                "command_schema": COMMAND_SCHEMA,
                "issue_url": (
                    f"https://github.com/{quote(manifest['repository']['owner'])}/"
                    f"{quote(manifest['repository']['name'])}/issues/new"
                    "?template=rapp-base-command.yml"
                ),
                "labels_authoritative": False,
                "one_command_per_issue": True,
                "title_prefix": REQUEST_TITLE_PREFIX,
                "transport": "github-issue-form",
            },
        },
        "collections": collection_entries,
        "entries": [
            {
                "name": "status",
                "schema": "rapp-base-status/1.0",
                "url": status_url,
            },
            {
                "name": "collections",
                "schema": "rapp-base-collections/1.0",
                "url": collections_url,
            },
            {
                "name": "events",
                "schema": "rapp-base-event-index/1.0",
                "url": events_url,
            },
            {
                "name": "receipts",
                "schema": "rapp-base-receipt-index/1.0",
                "url": receipts_url,
            },
            {
                "name": "versions",
                "schema": "rapp-base-version-index/1.0",
                "url": versions_url,
            },
        ],
        "events_url": events_url,
        "generated": generated_at,
        "generated_at": generated_at,
        "generator": BUILDER_PROFILE,
        "generation_sha256": generation,
        "immutable_request_versions": request_entries,
        "name": manifest["name"],
        "pages_base": pages_base,
        "profile": PROFILE,
        "raw_base": raw_base,
        "receipts_url": receipts_url,
        "repository": manifest["repository"],
        "status_url": status_url,
        "summary": {
            "collections": len(collection_entries),
            "events": len(state.events),
            "receipts": len(receipts),
            "requests": len(requests),
        },
        "versions_url": versions_url,
    }
    mutable["registry.json"] = canonical_bytes(registry)

    version_entries = _prepare_versions(root, manifest, immutable, write=write)
    version_index = {
        "schema": "rapp-base-version-index/1.0",
        "entries": version_entries,
        "generation_sha256": generation,
        "totalItems": len(version_entries),
    }
    mutable["versions/index.json"] = canonical_bytes(version_index)

    projection_paths = sorted(
        path
        for path in mutable
        if path == "registry.json" or path.startswith(f"{API_PREFIX}/")
    )
    mutable[f"{API_PREFIX}/build-index.json"] = canonical_bytes(
        {
            "schema": "rapp-base-build-index/1.0",
            "generation_sha256": generation,
            "paths": projection_paths,
        }
    )
    changed = head_changed + _write_mutable(root, manifest, mutable, write=write)
    return {
        "changed": changed,
        "collections": len(collection_entries),
        "events": len(state.events),
        "generation_sha256": generation,
        "records": sum(len(records) for records in state.records.values()),
        "receipts": len(receipts),
        "requests": len(requests),
        "versions": len(version_entries),
    }


def _generation_hash(
    manifest: dict[str, Any],
    state: ReplayState,
    requests: dict[int, dict[str, Any]],
    receipts: dict[int, dict[str, Any]],
) -> str:
    return object_hash(
        {
            "builder": BUILDER_PROFILE,
            "head": state.head,
            "manifest_sha256": object_hash(manifest),
            "receipts": sorted(
                receipt["receipt_id"] for receipt in receipts.values()
            ),
            "requests": sorted(
                request["request_hash"] for request in requests.values()
            ),
        }
    )


def _semantic_timestamp(
    manifest: dict[str, Any],
    state: ReplayState,
    requests: dict[int, dict[str, Any]],
) -> str:
    candidates: list[tuple[Any, int, str]] = [
        (timestamp_instant(manifest["generated_at"]), -1, manifest["generated_at"])
    ]
    candidates.extend(
        (
            timestamp_instant(request["admitted_at"]),
            request["issue"]["id"],
            request["admitted_at"],
        )
        for request in requests.values()
    )
    candidates.extend(
        (
            timestamp_instant(event["occurred_at"]),
            event["source"]["issue_id"],
            event["occurred_at"],
        )
        for event in state.events
    )
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _raw_base(manifest: dict[str, Any]) -> str:
    repository = manifest["repository"]
    return (
        "https://raw.githubusercontent.com/"
        f"{quote(repository['owner'], safe='')}/"
        f"{quote(repository['name'], safe='')}/"
        f"{quote(repository['branch'], safe='')}"
    )


def _pages_base(manifest: dict[str, Any]) -> str:
    repository = manifest["repository"]
    return (
        f"https://{quote(repository['owner'], safe='')}.github.io/"
        f"{quote(repository['name'], safe='')}/"
    )


def _request_sort_key(request: dict[str, Any]) -> tuple[int]:
    return (request["admission_sequence"],)


def _queue_immutable(
    versions: dict[str, tuple[bytes, str, str]],
    path: str,
    data: bytes,
    kind: str,
    semantic_hash: str,
) -> None:
    value = (data, kind, semantic_hash)
    previous = versions.get(path)
    if previous is not None and previous != value:
        raise RappError(
            "version_prefix_collision",
            f"12-character version prefix collision at {path}",
        )
    versions[path] = value


def _version_limits(manifest: dict[str, Any]) -> dict[str, int]:
    entry_ceiling = (
        64 * (HARD_LIMITS["records_per_collection"] + 1)
        + HARD_LIMITS["events"]
        + HARD_LIMITS["requests"]
    )
    return {
        **HARD_LIMITS,
        "array_items": entry_ceiling,
        "json_nodes": entry_ceiling * 6 + 32,
        "object_keys": entry_ceiling * 4 + 32,
    }


def _prepare_versions(
    root: Path,
    manifest: dict[str, Any],
    desired: dict[str, tuple[bytes, str, str]],
    *,
    write: bool,
) -> list[dict[str, Any]]:
    index_path = root / "versions" / "index.json"
    prior: dict[str, dict[str, Any]] = {}
    if index_path.exists():
        index = load_json_file(
            index_path,
            _version_limits(manifest),
            byte_limit=128 * 1024 * 1024,
        )
        expect_keys(
            index,
            required={"schema", "entries", "generation_sha256", "totalItems"},
            context="version index",
        )
        if index["schema"] != "rapp-base-version-index/1.0":
            raise RappError("invalid_version_index", "version index schema is invalid")
        if not isinstance(index["entries"], list):
            raise RappError("invalid_version_index", "version entries must be an array")
        for entry in index["entries"]:
            _validate_version_entry(entry)
            if entry["path"] in prior:
                raise RappError("invalid_version_index", "duplicate version path")
            path = root / entry["path"]
            if not path.is_file():
                raise RappError(
                    "version_disappeared", f"indexed version disappeared: {entry['path']}"
                )
            if sha256_bytes(path.read_bytes()) != entry["content_sha256"]:
                raise RappError(
                    "immutable_conflict", f"version content changed: {entry['path']}"
                )
            prior[entry["path"]] = entry
    existing_files = {
        path.relative_to(root).as_posix()
        for path in (root / "versions").glob("**/*.json")
        if path != index_path
    }
    unindexed = sorted(existing_files - prior.keys())
    for relative in unindexed:
        expected = desired.get(relative)
        if expected is None or (root / relative).read_bytes() != expected[0]:
            raise RappError(
                "unindexed_version",
                f"unindexed version is not an exact deterministic build artifact: {relative}",
            )

    result = dict(prior)
    for relative, (data, kind, semantic_hash) in sorted(desired.items()):
        content_hash = sha256_bytes(data)
        entry = {
            "content_sha256": content_hash,
            "kind": kind,
            "path": relative,
            "semantic_sha256": semantic_hash,
        }
        previous = prior.get(relative)
        if previous is not None and previous != entry:
            raise RappError("immutable_conflict", f"version index changed: {relative}")
        target = root / relative
        if target.exists() and target.read_bytes() != data:
            raise RappError("immutable_conflict", f"version content changed: {relative}")
        if write:
            write_bytes_immutable(target, data)
        elif not target.exists():
            raise RappError("build_out_of_date", f"missing version: {relative}")
        result[relative] = entry
    return [result[path] for path in sorted(result)]


def _validate_version_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise RappError("invalid_version_index", "version entry must be an object")
    expect_keys(
        entry,
        required={"content_sha256", "kind", "path", "semantic_sha256"},
        context="version entry",
    )
    require_hash(entry["content_sha256"], "version content_sha256")
    require_hash(entry["semantic_sha256"], "version semantic_sha256")
    if entry["kind"] not in {"record", "request", "collection"}:
        raise RappError("invalid_version_index", "version kind is invalid")
    pure = PurePosixPath(entry["path"])
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in entry["path"]
        or not entry["path"].startswith("versions/")
        or entry["path"] == "versions/index.json"
    ):
        raise RappError("invalid_version_index", "version path is unsafe")
    if not pure.name.endswith(".json"):
        raise RappError("invalid_version_index", "version path is invalid")
    if pure.stem != entry["content_sha256"][:12]:
        raise RappError("invalid_version_index", "version path hash prefix is invalid")
    expected_parts = {"record": 5, "request": 3, "collection": 4}
    expected_root = {
        "record": "records",
        "request": "requests",
        "collection": "collections",
    }
    if (
        len(pure.parts) != expected_parts[entry["kind"]]
        or pure.parts[1] != expected_root[entry["kind"]]
    ):
        raise RappError("invalid_version_index", "version path shape is invalid")


def _write_mutable(
    root: Path,
    manifest: dict[str, Any],
    desired: dict[str, bytes],
    *,
    write: bool,
) -> int:
    old_paths: set[str] = set()
    build_index_path = root / API_PREFIX / "build-index.json"
    if build_index_path.exists():
        old_index = load_json_file(
            build_index_path,
            _version_limits(manifest),
            byte_limit=32 * 1024 * 1024,
        )
        if (
            isinstance(old_index, dict)
            and old_index.get("schema") == "rapp-base-build-index/1.0"
            and isinstance(old_index.get("paths"), list)
        ):
            for relative in old_index["paths"]:
                if not isinstance(relative, str):
                    raise RappError("invalid_build_index", "build path is invalid")
                _validate_projection_path(relative)
                old_paths.add(relative)
        else:
            raise RappError("invalid_build_index", "build index is invalid")
    desired_paths = set(desired)
    obsolete = sorted(old_paths - desired_paths)
    changed = 0
    for relative in obsolete:
        _validate_projection_path(relative)
        target = root / relative
        if target.exists():
            if not write:
                raise RappError("build_out_of_date", f"obsolete projection: {relative}")
            target.unlink()
            changed += 1
    for relative, data in sorted(desired.items()):
        if relative != "versions/index.json":
            _validate_projection_path(relative)
        target = root / relative
        if write:
            changed += int(write_bytes_atomic(target, data))
        elif not target.exists() or target.read_bytes() != data:
            raise RappError("build_out_of_date", f"projection differs: {relative}")
    if write:
        _prune_empty(root / API_PREFIX)
    return changed


def _validate_projection_path(relative: str) -> None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise RappError("unsafe_generated_path", "generated path is unsafe")
    if relative != "registry.json" and not relative.startswith(f"{API_PREFIX}/"):
        raise RappError(
            "unsafe_generated_path",
            f"generated path is outside {API_PREFIX}; other API majors are isolated",
        )


def _prune_empty(directory: Path) -> None:
    if not directory.exists():
        return
    for path in sorted(
        (item for item in directory.glob("**/*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
