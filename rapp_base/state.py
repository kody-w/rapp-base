"""Canonical records and append-only event-ledger replay."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import (
    HARD_LIMITS,
    POLICIES_CREATE,
    POLICIES_MUTATE,
    REQUEST_ENVELOPE_BYTES,
    REQUEST_ENVELOPE_LIMITS,
    SEGMENT_PATTERN,
    ZERO_HASH,
)
from .errors import RappError
from .jsonutil import (
    ensure_safe_segment,
    expect_keys,
    latest_timestamp,
    load_json_file,
    normalize_timestamp,
    object_hash,
    require_canonical_json,
    require_hash,
)
from .manifest import (
    assert_unique,
    authorize,
    collection_map,
    genesis_sha256,
    validate_actor_id,
    validate_data,
)

_EVENT_FILE_RE = re.compile(r"^(?P<sequence>[0-9]{8})-(?P<prefix>[0-9a-f]{12})\.json$")


@dataclass
class ReplayState:
    records: dict[str, dict[str, dict[str, Any]]]
    history: dict[tuple[str, str], list[dict[str, Any]]]
    events: list[dict[str, Any]]
    head: dict[str, Any]


def _revised(body: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(body)
    result["revision"] = object_hash(result)
    return result


def make_live_record(
    collection: str,
    record_id: str,
    owner_id: int,
    created_at: str,
    updated_at: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return _revised(
        {
            "schema": "rapp-base-record/1.0",
            "collection": collection,
            "created_at": created_at,
            "data": copy.deepcopy(data),
            "deleted": False,
            "id": record_id,
            "owner_id": owner_id,
            "updated_at": updated_at,
        }
    )


def make_tombstone(record: dict[str, Any], deleted_at: str) -> dict[str, Any]:
    semantic_time = latest_timestamp(record["updated_at"], deleted_at)
    return _revised(
        {
            "schema": "rapp-base-tombstone/1.0",
            "collection": record["collection"],
            "created_at": record["created_at"],
            "deleted": True,
            "deleted_at": semantic_time,
            "id": record["id"],
            "owner_id": record["owner_id"],
            "prior_revision": record["revision"],
            "updated_at": semantic_time,
        }
    )


def deterministic_record_id(
    *,
    repository_id: int,
    issue_id: int,
    issue_node_id: str,
    command_id: str,
    collection: str,
) -> str:
    identity = {
        "collection": collection,
        "command_id": command_id,
        "issue_id": issue_id,
        "issue_node_id": issue_node_id,
        "repository_id": repository_id,
    }
    return "r_" + object_hash(identity)[:24]


def seed_state(manifest: dict[str, Any]) -> ReplayState:
    genesis_hash = genesis_sha256(manifest)
    records: dict[str, dict[str, dict[str, Any]]] = {}
    history: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for collection in manifest["collections"]:
        collection_records: dict[str, dict[str, Any]] = {}
        records[collection["name"]] = collection_records
        for seed in collection["seed"]:
            record = make_live_record(
                collection["name"],
                seed["id"],
                seed["owner_id"],
                seed["created_at"],
                seed["created_at"],
                validate_data(collection, seed["data"]),
            )
            assert_unique(collection, collection_records, record["data"])
            collection_records[record["id"]] = record
            history[(collection["name"], record["id"])] = [copy.deepcopy(record)]
    return ReplayState(
        records=records,
        history=history,
        events=[],
        head={
            "schema": "rapp-base-head/1.0",
            "event_hash": None,
            "event_path": None,
            "genesis_sha256": genesis_hash,
            "sequence": 0,
        },
    )


def replay(
    root: Path, manifest: dict[str, Any], *, repair_head: bool = False
) -> ReplayState:
    """Replay events; write-capable callers may tolerate a repairable stale head."""

    state = seed_state(manifest)
    genesis_hash = state.head["genesis_sha256"]
    events_dir = root / "state" / "events"
    event_paths = sorted(events_dir.glob("*.json")) if events_dir.exists() else []
    if len(event_paths) > HARD_LIMITS["events"]:
        raise RappError("event_limit", "event ledger exceeds its hard limit")
    previous_hash = ZERO_HASH
    for expected_sequence, path in enumerate(event_paths, start=1):
        match = _EVENT_FILE_RE.fullmatch(path.name)
        if match is None:
            raise RappError("invalid_event_path", f"invalid event filename: {path.name}")
        if int(match.group("sequence")) != expected_sequence:
            raise RappError("event_sequence", "event ledger sequence is not contiguous")
        event = load_json_file(
            path,
            REQUEST_ENVELOPE_LIMITS,
            byte_limit=REQUEST_ENVELOPE_BYTES,
        )
        require_canonical_json(path, event, "event")
        _validate_event_envelope(event)
        if event["genesis_sha256"] != genesis_hash:
            raise RappError(
                "migration_required",
                "manifest schemas or seeds differ from the anchored v1 genesis; "
                "start a new API major/repository or implement an explicit migration",
            )
        if event["sequence"] != expected_sequence:
            raise RappError("event_sequence", "event sequence does not match filename")
        if event["previous_hash"] != previous_hash:
            raise RappError("event_chain", "event previous hash does not match")
        calculated = object_hash(event, "hash")
        if event["hash"] != calculated:
            raise RappError("event_hash", "event hash does not match its content")
        if not event["hash"].startswith(match.group("prefix")):
            raise RappError("event_hash", "event filename does not match its hash")
        result = apply_event(state, manifest, event)
        if result["revision"] != event["result_revision"]:
            raise RappError("event_result", "event result revision is invalid")
        advance_state(state, event, result)
        previous_hash = event["hash"]
    state.head = _load_and_validate_head(
        root,
        manifest,
        state.events,
        repair=repair_head,
    )
    return state


def _load_and_validate_head(
    root: Path,
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    repair: bool,
) -> dict[str, Any]:
    path = root / "state" / "head.json"
    if not path.exists():
        raise RappError("missing_head", "state/head.json is required")
    head = load_json_file(path, REQUEST_ENVELOPE_LIMITS, byte_limit=4096)
    require_canonical_json(path, head, "head")
    expect_keys(
        head,
        required={
            "schema",
            "sequence",
            "event_hash",
            "event_path",
            "genesis_sha256",
        },
        context="head",
    )
    if head["schema"] != "rapp-base-head/1.0":
        raise RappError("invalid_head", "head schema is invalid")
    require_hash(head["genesis_sha256"], "head genesis_sha256")
    sequence = head["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise RappError("invalid_head", "head sequence is invalid")
    if head["event_hash"] is not None:
        require_hash(head["event_hash"], "head event_hash")
    if head["event_path"] is not None and not isinstance(head["event_path"], str):
        raise RappError("invalid_head", "head event path is invalid")
    expected = head_for_events(manifest, events)
    if head == expected:
        return head
    if head["genesis_sha256"] != expected["genesis_sha256"]:
        if events:
            raise RappError(
                "migration_required",
                "state genesis does not match replay-critical manifest schemas or seeds",
            )
        if repair:
            return expected
        raise RappError("stale_head", "zero-event head needs deterministic re-anchoring")
    if sequence > len(events):
        raise RappError("invalid_head", "head is ahead of the event ledger")
    if sequence == 0:
        lagging_expected = (None, None)
    else:
        event = events[sequence - 1]
        lagging_expected = (
            event["hash"],
            event_filename(event["sequence"], event["hash"]),
        )
    if (head["event_hash"], head["event_path"]) != lagging_expected:
        raise RappError("invalid_head", "head forks from the event ledger")
    if repair:
        return expected
    raise RappError("stale_head", "head lags the authoritative contiguous event ledger")


def head_for_events(
    manifest: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    if events:
        last = events[-1]
        sequence = last["sequence"]
        event_hash = last["hash"]
        event_path = event_filename(sequence, event_hash)
    else:
        sequence = 0
        event_hash = None
        event_path = None
    return {
        "schema": "rapp-base-head/1.0",
        "event_hash": event_hash,
        "event_path": event_path,
        "genesis_sha256": genesis_sha256(manifest),
        "sequence": sequence,
    }


def _validate_event_envelope(event: dict[str, Any]) -> None:
    expect_keys(
        event,
        required={
            "schema",
            "genesis_sha256",
            "sequence",
            "previous_hash",
            "hash",
            "request_hash",
            "command_id",
            "operation",
            "collection",
            "record_id",
            "actor",
            "source",
            "occurred_at",
            "policy",
            "authorization",
            "if_revision",
            "data",
            "result_revision",
        },
        context="event",
    )
    if event["schema"] != "rapp-base-event/1.0":
        raise RappError("invalid_event", "event schema is invalid")
    if (
        isinstance(event["sequence"], bool)
        or not isinstance(event["sequence"], int)
        or event["sequence"] < 1
    ):
        raise RappError("invalid_event", "event sequence is invalid")
    require_hash(event["previous_hash"], "event previous_hash")
    require_hash(event["genesis_sha256"], "event genesis_sha256")
    require_hash(event["hash"], "event hash")
    require_hash(event["request_hash"], "event request_hash")
    require_hash(event["result_revision"], "event result_revision")
    ensure_safe_segment(event["collection"], "event collection", r"^[a-z][a-z0-9_-]{0,62}$")
    ensure_safe_segment(event["record_id"], "event record_id", SEGMENT_PATTERN)
    if event["operation"] not in {"create", "update", "delete"}:
        raise RappError("invalid_event", "event operation is invalid")
    if not isinstance(event["command_id"], str):
        raise RappError("invalid_event", "event command id is invalid")
    if not isinstance(event["actor"], dict):
        raise RappError("invalid_event", "event actor is invalid")
    expect_keys(
        event["actor"], required={"id", "association"}, context="event actor"
    )
    validate_actor_id(event["actor"]["id"])
    if not isinstance(event["actor"]["association"], str):
        raise RappError("invalid_event", "event association is invalid")
    if not isinstance(event["source"], dict):
        raise RappError("invalid_event", "event source is invalid")
    expect_keys(
        event["source"],
        required={"repository_id", "issue_id", "issue_node_id"},
        context="event source",
    )
    for key in ("repository_id", "issue_id"):
        value = event["source"][key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > 9_007_199_254_740_991
        ):
            raise RappError("invalid_event", f"event {key} is invalid")
    if (
        not isinstance(event["source"]["issue_node_id"], str)
        or not event["source"]["issue_node_id"]
    ):
        raise RappError("invalid_event", "event issue node id is invalid")
    event["occurred_at"] = normalize_timestamp(
        event["occurred_at"], "event occurred_at"
    )
    if not isinstance(event["authorization"], str):
        raise RappError("invalid_event", "event authorization is invalid")


def apply_event(
    state: ReplayState,
    manifest: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    collections = collection_map(manifest)
    collection = collections.get(event["collection"])
    if collection is None:
        raise RappError("unknown_collection", "event references an unknown collection")
    records = state.records[event["collection"]]
    operation = event["operation"]
    policy_choices = POLICIES_CREATE if operation == "create" else POLICIES_MUTATE
    if event["policy"] not in policy_choices:
        raise RappError("invalid_event", "event policy snapshot is invalid")

    if operation == "create":
        if event["if_revision"] is not None:
            raise RappError("invalid_event", "create event cannot have if_revision")
        expected_id = deterministic_record_id(
            repository_id=event["source"]["repository_id"],
            issue_id=event["source"]["issue_id"],
            issue_node_id=event["source"]["issue_node_id"],
            command_id=event["command_id"],
            collection=event["collection"],
        )
        if event["record_id"] != expected_id:
            raise RappError("invalid_event", "create record id is not deterministic")
        if event["record_id"] in records:
            raise RappError("record_exists", "event would recreate an existing record id")
        data = validate_data(collection, event["data"])
        assert_unique(collection, records, data)
        authorization = authorize(
            event["policy"],
            actor_id=event["actor"]["id"],
            association=event["actor"]["association"],
        )
        record = make_live_record(
            event["collection"],
            event["record_id"],
            event["actor"]["id"],
            event["occurred_at"],
            event["occurred_at"],
            data,
        )
    else:
        current = records.get(event["record_id"])
        if current is None or current["deleted"]:
            raise RappError("not_found", "record does not exist")
        if event["if_revision"] != current["revision"]:
            raise RappError("stale_revision", "record revision does not match")
        authorization = authorize(
            event["policy"],
            actor_id=event["actor"]["id"],
            association=event["actor"]["association"],
            owner_id=current["owner_id"],
        )
        if operation == "update":
            if not isinstance(event["data"], dict):
                raise RappError("invalid_event", "update data must be an object")
            merged = copy.deepcopy(current["data"])
            merged.update(event["data"])
            if merged == current["data"]:
                raise RappError("no_change", "update does not change record data")
            data = validate_data(collection, merged)
            assert_unique(
                collection,
                records,
                data,
                exclude_id=event["record_id"],
            )
            record = make_live_record(
                event["collection"],
                event["record_id"],
                current["owner_id"],
                current["created_at"],
                latest_timestamp(current["updated_at"], event["occurred_at"]),
                data,
            )
        else:
            if event["data"] is not None:
                raise RappError("invalid_event", "delete event data must be null")
            record = make_tombstone(current, event["occurred_at"])
    if authorization != event["authorization"]:
        raise RappError("invalid_event", "event authorization proof is invalid")
    records[event["record_id"]] = record
    return record


def event_filename(sequence: int, event_hash: str) -> str:
    return f"{sequence:08d}-{event_hash[:12]}.json"


def advance_state(
    state: ReplayState,
    event: dict[str, Any],
    record: dict[str, Any],
) -> None:
    state.events.append(copy.deepcopy(event))
    state.history.setdefault((event["collection"], event["record_id"]), []).append(
        copy.deepcopy(record)
    )
    state.head = {
        "schema": "rapp-base-head/1.0",
        "event_hash": event["hash"],
        "event_path": event_filename(event["sequence"], event["hash"]),
        "genesis_sha256": event["genesis_sha256"],
        "sequence": event["sequence"],
    }
