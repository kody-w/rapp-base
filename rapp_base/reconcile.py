"""Pure reconciliation from trusted, normalized GitHub metadata."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .commands import (
    admit_request,
    is_request_issue,
    normalize_issue,
    normalize_repository,
    parse_command_text,
    request_filename,
)
from .constants import (
    HARD_LIMITS,
    KNOWN_ASSOCIATIONS,
    LIMIT_KEYS,
    PARSER_PROFILE,
    POLICIES_CREATE,
    POLICIES_MUTATE,
    REQUEST_ENVELOPE_BYTES,
    REQUEST_ENVELOPE_LIMITS,
    REQUEST_TITLE_PREFIX,
    ZERO_HASH,
)
from .errors import RappError, is_command_rejection, public_error_message
from .jsonutil import (
    expect_keys,
    load_json_file,
    normalize_timestamp,
    object_hash,
    require_canonical_json,
    require_hash,
    sha256_bytes,
    timestamp_instant,
    write_json_atomic,
    write_json_immutable,
)
from .manifest import authorize, collection_map, validate_actor_id
from .state import (
    ReplayState,
    advance_state,
    apply_event,
    deterministic_record_id,
    event_filename,
    replay,
    seed_state,
)

_REQUEST_FILE_RE = re.compile(r"^issue-(?P<id>[0-9]+)\.json$")
_RECEIPT_FILE_RE = re.compile(r"^issue-(?P<id>[0-9]+)\.json$")


def reconcile_document(
    root: Path,
    manifest: dict[str, Any],
    document: dict[str, Any],
) -> dict[str, Any]:
    expect_keys(
        document,
        required={"repository", "issues"},
        context="reconciliation input",
    )
    repository = normalize_repository(document["repository"])
    _verify_repository(manifest, repository)
    raw_issues = document["issues"]
    if not isinstance(raw_issues, list):
        raise RappError("invalid_provenance", "issues must be an array")
    if len(raw_issues) > manifest["limits"]["issues_per_reconcile"]:
        raise RappError("issue_limit", "reconciliation input has too many issues")

    normalized: list[dict[str, Any]] = []
    provenance_errors: list[dict[str, Any]] = []
    identities: dict[int, str] = {}
    for index, raw_issue in enumerate(raw_issues):
        try:
            issue = normalize_issue(raw_issue)
            previous_node = identities.get(issue["id"])
            if previous_node is not None and previous_node != issue["node_id"]:
                raise RappError(
                    "invalid_provenance", "issue database id has conflicting node ids"
                )
            identities[issue["id"]] = issue["node_id"]
            if is_request_issue(issue):
                normalized.append(issue)
        except RappError as exc:
            provenance_errors.append(
                {"code": exc.code, "index": index, "message": exc.message}
            )
    normalized.sort(
        key=lambda issue: (timestamp_instant(issue["created_at"]), issue["id"])
    )

    requests = load_requests(root, manifest)
    receipts = load_receipts(root, manifest)
    state = replay(root, manifest, repair_head=True)
    result = {
        "admitted": 0,
        "applied": 0,
        "noop": 0,
        "rejected": 0,
        "existing": 0,
        "provenance_errors": provenance_errors,
    }
    for issue in normalized:
        request_path = root / "state" / "requests" / request_filename(issue["id"])
        request = requests.get(issue["id"])
        if request is None:
            if len(requests) >= manifest["limits"]["requests"]:
                raise RappError("request_limit", "request ledger limit reached")
            request = admit_request(
                repository,
                issue,
                manifest,
                admission_sequence=len(requests) + 1,
            )
            write_json_immutable(request_path, request)
            requests[issue["id"]] = request
            result["admitted"] += 1
        else:
            result["existing"] += 1
    newly_terminal = _materialize_history(
        root,
        manifest,
        requests,
        receipts,
        state,
    )
    for receipt in newly_terminal:
        result[receipt["status"]] += 1
    return result


def _verify_repository(
    manifest: dict[str, Any], repository: dict[str, Any]
) -> None:
    configured = manifest["repository"]
    if repository["full_name"].split("/", 1)[1].lower() != configured["name"].lower():
        raise RappError("repository_mismatch", "GitHub repository does not match manifest")
    expected = f"{configured['owner']}/{configured['name']}"
    if repository["full_name"].lower() != expected.lower():
        raise RappError(
            "repository_mismatch", "GitHub repository does not match manifest"
        )


def load_requests(
    root: Path, manifest: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    directory = root / "state" / "requests"
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    if len(paths) > HARD_LIMITS["requests"]:
        raise RappError("request_limit", "request ledger exceeds its hard limit")
    for path in paths:
        match = _REQUEST_FILE_RE.fullmatch(path.name)
        if match is None:
            raise RappError("invalid_request_path", f"invalid request path: {path.name}")
        request = load_json_file(
            path,
            REQUEST_ENVELOPE_LIMITS,
            byte_limit=REQUEST_ENVELOPE_BYTES,
            allow_control_characters=True,
        )
        require_canonical_json(path, request, "request")
        _validate_request(request)
        issue_id = int(match.group("id"))
        if request["issue"]["id"] != issue_id:
            raise RappError("invalid_request", "request filename identity mismatch")
        if issue_id in result:
            raise RappError("invalid_request", "duplicate request identity")
        result[issue_id] = request
    sequences = sorted(request["admission_sequence"] for request in result.values())
    if sequences != list(range(1, len(result) + 1)):
        raise RappError(
            "invalid_request", "request admission sequence is not contiguous"
        )
    return result


def _validate_request(request: dict[str, Any]) -> None:
    expect_keys(
        request,
        required={
            "schema",
            "admission",
            "request_hash",
            "actor",
            "admission_sequence",
            "admitted_at",
            "body_sha256",
            "command",
            "command_sha256",
            "command_text",
            "issue",
            "parse_error",
            "repository",
        },
        context="request",
    )
    if request["schema"] != "rapp-base-request/1.0":
        raise RappError("invalid_request", "request schema is invalid")
    _validate_admission_snapshot(request["admission"], request.get("command"))
    if (
        isinstance(request["admission_sequence"], bool)
        or not isinstance(request["admission_sequence"], int)
        or request["admission_sequence"] < 1
    ):
        raise RappError("invalid_request", "request admission sequence is invalid")
    require_hash(request["request_hash"], "request_hash")
    require_hash(request["body_sha256"], "body_sha256")
    if object_hash(request, "request_hash") != request["request_hash"]:
        raise RappError("invalid_request", "request hash does not match")
    if normalize_repository(request["repository"]) != request["repository"]:
        raise RappError("invalid_request", "request repository identity is invalid")
    if normalize_timestamp(request["admitted_at"], "request admitted_at") != request[
        "admitted_at"
    ]:
        raise RappError("invalid_request", "request timestamp is not canonical")
    if not isinstance(request["issue"], dict):
        raise RappError("invalid_request", "request issue identity is invalid")
    expect_keys(
        request["issue"],
        required={"id", "node_id", "number", "title"},
        context="request issue",
    )
    for field in ("id", "number"):
        value = request["issue"][field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > 9_007_199_254_740_991
        ):
            raise RappError("invalid_request", "request issue identity is invalid")
    if (
        not isinstance(request["issue"]["node_id"], str)
        or not request["issue"]["node_id"]
        or any(ord(character) < 32 for character in request["issue"]["node_id"])
    ):
        raise RappError("invalid_request", "request issue node id is invalid")
    if (
        not isinstance(request["issue"]["title"], str)
        or not request["issue"]["title"]
        or not request["issue"]["title"].startswith(REQUEST_TITLE_PREFIX)
        or len(request["issue"]["title"].encode("utf-8", errors="strict")) > 1024
        or any(ord(character) < 32 for character in request["issue"]["title"])
    ):
        raise RappError("invalid_request", "request issue title is invalid")
    if not isinstance(request["actor"], dict):
        raise RappError("invalid_request", "request actor is invalid")
    expect_keys(
        request["actor"], required={"id", "association"}, context="request actor"
    )
    validate_actor_id(request["actor"]["id"])
    if request["actor"]["association"] not in KNOWN_ASSOCIATIONS:
        raise RappError("invalid_request", "request association is invalid")
    if request["command"] is None:
        if not isinstance(request["parse_error"], dict):
            raise RappError("invalid_request", "invalid request lacks parse error")
        expect_keys(
            request["parse_error"],
            required={"code", "message"},
            context="request parse error",
        )
        if not all(
            isinstance(request["parse_error"][key], str)
            and request["parse_error"][key]
            for key in ("code", "message")
        ):
            raise RappError("invalid_request", "request parse error is invalid")
        if (
            len(request["parse_error"]["message"]) > 500
            or any(
                ord(character) < 32
                for character in request["parse_error"]["message"]
            )
        ):
            raise RappError("invalid_request", "request parse error is unsafe")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", request["parse_error"]["code"]) is None:
            raise RappError("invalid_request", "request parse error code is invalid")
        if request["command_text"] is None:
            if request["command_sha256"] is not None:
                raise RappError("invalid_request", "request command hash is invalid")
            if request["parse_error"]["code"] not in {
                "body_too_large",
                "command_too_large",
                "empty_command",
                "invalid_body",
                "invalid_issue_form",
                "invalid_unicode",
            }:
                raise RappError("invalid_request", "request extraction error is invalid")
        else:
            if not isinstance(request["command_text"], str):
                raise RappError("invalid_request", "request command text is invalid")
            require_hash(request["command_sha256"], "command_sha256")
            if (
                sha256_bytes(request["command_text"].encode("utf-8"))
                != request["command_sha256"]
            ):
                raise RappError("invalid_request", "request command hash does not match")
            try:
                parse_command_text(
                    request["command_text"],
                    request["admission"]["limits"],
                )
            except RappError as exc:
                if (
                    exc.code != request["parse_error"]["code"]
                    or public_error_message(exc.code)
                    != request["parse_error"]["message"]
                ):
                    raise RappError(
                        "invalid_request", "request parse error does not reproduce"
                    ) from exc
            else:
                raise RappError("invalid_request", "request parse error is invalid")
    else:
        if (
            not isinstance(request["command"], dict)
            or not isinstance(request["command_text"], str)
            or request["command_sha256"] is None
        ):
            raise RappError("invalid_request", "request command snapshot is invalid")
        require_hash(request["command_sha256"], "command_sha256")
        if (
            sha256_bytes(request["command_text"].encode("utf-8"))
            != request["command_sha256"]
        ):
            raise RappError("invalid_request", "request command hash does not match")
        if parse_command_text(
            request["command_text"],
            request["admission"]["limits"],
        ) != request["command"]:
            raise RappError("invalid_request", "request command snapshot does not match")
        if request["parse_error"] is not None:
            raise RappError("invalid_request", "valid request has a parse error")


def _validate_admission_snapshot(
    admission: Any, command: dict[str, Any] | None
) -> None:
    if not isinstance(admission, dict):
        raise RappError("invalid_request", "request admission snapshot is invalid")
    expect_keys(
        admission,
        required={"limits", "parser_profile", "policy"},
        context="request admission snapshot",
    )
    if admission["parser_profile"] != PARSER_PROFILE:
        raise RappError("invalid_request", "request parser profile is invalid")
    limits = admission["limits"]
    if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS:
        raise RappError("invalid_request", "request admission limits are invalid")
    for key, ceiling in HARD_LIMITS.items():
        value = limits[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > ceiling
        ):
            raise RappError("invalid_request", "request admission limits are unsafe")
    if limits["snapshot_items"] > limits["records_per_collection"]:
        raise RappError("invalid_request", "request admission limits are inconsistent")
    policy = admission["policy"]
    if policy is not None:
        if not isinstance(command, dict) or command.get("operation") not in {
            "create",
            "update",
            "delete",
        }:
            raise RappError("invalid_request", "invalid command cannot snapshot a policy")
        choices = (
            POLICIES_CREATE
            if command["operation"] == "create"
            else POLICIES_MUTATE
        )
        if policy not in choices:
            raise RappError("invalid_request", "request policy snapshot is invalid")


def load_receipts(
    root: Path, manifest: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    directory = root / "state" / "receipts"
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    if len(paths) > HARD_LIMITS["requests"]:
        raise RappError("request_limit", "receipt ledger exceeds its hard limit")
    for path in paths:
        match = _RECEIPT_FILE_RE.fullmatch(path.name)
        if match is None:
            raise RappError("invalid_receipt_path", f"invalid receipt path: {path.name}")
        receipt = load_json_file(
            path,
            REQUEST_ENVELOPE_LIMITS,
            byte_limit=REQUEST_ENVELOPE_BYTES,
        )
        require_canonical_json(path, receipt, "receipt")
        _validate_receipt(receipt)
        issue_id = int(match.group("id"))
        if receipt["issue"]["id"] != issue_id:
            raise RappError("invalid_receipt", "receipt filename identity mismatch")
        result[issue_id] = receipt
    return result


def _validate_receipt(receipt: dict[str, Any]) -> None:
    expect_keys(
        receipt,
        required={
            "schema",
            "receipt_id",
            "request_hash",
            "command_id",
            "issue",
            "actor_id",
            "status",
            "code",
            "message",
            "occurred_at",
            "event",
            "record",
            "duplicate_of",
        },
        context="receipt",
    )
    if receipt["schema"] != "rapp-base-receipt/1.0":
        raise RappError("invalid_receipt", "receipt schema is invalid")
    require_hash(receipt["receipt_id"], "receipt_id")
    require_hash(receipt["request_hash"], "request_hash")
    if object_hash(receipt, "receipt_id") != receipt["receipt_id"]:
        raise RappError("invalid_receipt", "receipt id does not match")
    if receipt["status"] not in {"applied", "rejected", "noop"}:
        raise RappError("invalid_receipt", "receipt status is invalid")
    if (
        isinstance(receipt["actor_id"], bool)
        or not isinstance(receipt["actor_id"], int)
        or receipt["actor_id"] < 1
    ):
        raise RappError("invalid_receipt", "receipt actor id is invalid")
    if receipt["command_id"] is not None and not isinstance(
        receipt["command_id"], str
    ):
        raise RappError("invalid_receipt", "receipt command id is invalid")
    if not isinstance(receipt["issue"], dict):
        raise RappError("invalid_receipt", "receipt issue is invalid")
    expect_keys(
        receipt["issue"],
        required={"id", "node_id", "number", "title"},
        context="receipt issue",
    )
    for field in ("id", "number"):
        value = receipt["issue"][field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > 9_007_199_254_740_991
        ):
            raise RappError("invalid_receipt", "receipt issue identity is invalid")
    for field in ("code", "message"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise RappError("invalid_receipt", f"receipt {field} is invalid")
        if len(receipt[field]) > 500 or any(
            ord(character) < 32 for character in receipt[field]
        ):
            raise RappError("invalid_receipt", f"receipt {field} is unsafe")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", receipt["code"]) is None:
        raise RappError("invalid_receipt", "receipt code is invalid")
    if normalize_timestamp(receipt["occurred_at"], "receipt occurred_at") != receipt[
        "occurred_at"
    ]:
        raise RappError("invalid_receipt", "receipt timestamp is not canonical")
    if receipt["status"] == "applied":
        if not isinstance(receipt["event"], dict) or not isinstance(
            receipt["record"], dict
        ):
            raise RappError("invalid_receipt", "applied receipt lacks event or record")
    elif receipt["event"] is not None or receipt["record"] is not None:
        raise RappError("invalid_receipt", "non-applied receipt cannot reference an event")
    if receipt["event"] is not None:
        expect_keys(
            receipt["event"],
            required={"hash", "path", "sequence"},
            context="receipt event",
        )
        require_hash(receipt["event"]["hash"], "receipt event hash")
    if receipt["record"] is not None:
        expect_keys(
            receipt["record"],
            required={"collection", "deleted", "id", "revision"},
            context="receipt record",
        )
        require_hash(receipt["record"]["revision"], "receipt record revision")
    if receipt["duplicate_of"] is not None:
        if not isinstance(receipt["duplicate_of"], dict):
            raise RappError("invalid_receipt", "receipt duplicate reference is invalid")
        expect_keys(
            receipt["duplicate_of"],
            required={"issue_id", "request_hash"},
            context="receipt duplicate reference",
        )
        require_hash(
            receipt["duplicate_of"]["request_hash"], "duplicate request hash"
        )


def _request_sort_key(request: dict[str, Any]) -> tuple[int]:
    return (request["admission_sequence"],)


def _event_code(operation: str) -> str:
    return {"create": "created", "update": "updated", "delete": "deleted"}[operation]


def derive_history(
    manifest: dict[str, Any],
    requests: dict[int, dict[str, Any]],
) -> tuple[ReplayState, dict[int, dict[str, Any]]]:
    """Purely re-derive every terminal outcome in admission order."""

    state = seed_state(manifest)
    expected_receipts: dict[int, dict[str, Any]] = {}
    prior_by_command: dict[str, dict[str, Any]] = {}
    for request in sorted(requests.values(), key=_request_sort_key):
        command = request["command"]
        if command is None:
            error = request["parse_error"]
            receipt = _make_receipt_v1(
                request,
                status="rejected",
                code=error["code"],
                message=error["message"],
            )
        else:
            prior = prior_by_command.get(command["command_id"])
            if prior is not None:
                identical = prior["command_sha256"] == request["command_sha256"]
                receipt = _make_receipt_v1(
                    request,
                    status="noop" if identical else "rejected",
                    code="identical_replay" if identical else "command_id_conflict",
                    message=(
                        "identical command_id replay; no event was appended"
                        if identical
                        else "command_id was already admitted with different command bytes"
                    ),
                    duplicate_of={
                        "issue_id": prior["issue"]["id"],
                        "request_hash": prior["request_hash"],
                    },
                )
            else:
                prior_by_command[command["command_id"]] = request
                try:
                    event, record = prepare_event(manifest, state, request)
                except RappError as exc:
                    if not is_command_rejection(exc.code):
                        raise
                    receipt = _make_receipt_v1(
                        request,
                        status="rejected",
                        code=exc.code,
                        message=public_error_message(exc.code),
                    )
                else:
                    receipt = _make_receipt_v1(
                        request,
                        status="applied",
                        code=_event_code(event["operation"]),
                        message=f"{event['operation']} was applied",
                        event=event,
                        record=record,
                    )
        expected_receipts[request["issue"]["id"]] = receipt
    return state, expected_receipts


def prepare_event(
    manifest: dict[str, Any],
    state: ReplayState,
    request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare and reduce one event without filesystem side effects."""

    limits = request["admission"]["limits"]
    if len(state.events) >= limits["events"]:
        raise RappError("event_limit", "event ledger limit reached")
    command = request["command"]
    collections = collection_map(manifest)
    collection = collections.get(command["collection"])
    policy = request["admission"]["policy"]
    if collection is None or policy is None:
        raise RappError("unknown_collection", "collection does not exist")
    operation = command["operation"]
    records = state.records[command["collection"]]
    if operation == "create":
        if len(records) >= limits["records_per_collection"]:
            raise RappError("record_limit", "collection record limit reached")
        record_id = deterministic_record_id(
            repository_id=request["repository"]["id"],
            issue_id=request["issue"]["id"],
            issue_node_id=request["issue"]["node_id"],
            command_id=command["command_id"],
            collection=command["collection"],
        )
        owner_id = None
    else:
        record_id = command["record_id"]
        current = records.get(record_id)
        if current is None or current["deleted"]:
            raise RappError("not_found", "record does not exist")
        if command["if_revision"] != current["revision"]:
            raise RappError("stale_revision", "record revision does not match")
        owner_id = current["owner_id"]
    authorization = authorize(
        policy,
        actor_id=request["actor"]["id"],
        association=request["actor"]["association"],
        owner_id=owner_id,
    )
    sequence = state.head["sequence"] + 1
    event: dict[str, Any] = {
        "schema": "rapp-base-event/1.0",
        "actor": copy.deepcopy(request["actor"]),
        "authorization": authorization,
        "collection": command["collection"],
        "command_id": command["command_id"],
        "data": copy.deepcopy(command.get("data")),
        "genesis_sha256": state.head["genesis_sha256"],
        "hash": ZERO_HASH,
        "if_revision": command.get("if_revision"),
        "occurred_at": request["admitted_at"],
        "operation": operation,
        "policy": policy,
        "previous_hash": state.head["event_hash"] or ZERO_HASH,
        "record_id": record_id,
        "request_hash": request["request_hash"],
        "result_revision": ZERO_HASH,
        "sequence": sequence,
        "source": {
            "issue_id": request["issue"]["id"],
            "issue_node_id": request["issue"]["node_id"],
            "repository_id": request["repository"]["id"],
        },
    }
    record = apply_event(state, manifest, event)
    event["result_revision"] = record["revision"]
    event["hash"] = object_hash(event, "hash")
    advance_state(state, event, record)
    return event, record


def verify_history(
    manifest: dict[str, Any],
    requests: dict[int, dict[str, Any]],
    receipts: dict[int, dict[str, Any]],
    state: ReplayState,
) -> None:
    expected_state, expected_receipts = derive_history(manifest, requests)
    if state.events != expected_state.events:
        raise RappError(
            "history_mismatch",
            "event ledger is not the exact deterministic reduction of admitted requests",
        )
    if state.head != expected_state.head:
        raise RappError("history_mismatch", "head is not the deterministic event head")
    if receipts != expected_receipts:
        raise RappError(
            "history_mismatch",
            "receipt ledger is not the exact deterministic reduction of admitted requests",
        )


def _materialize_history(
    root: Path,
    manifest: dict[str, Any],
    requests: dict[int, dict[str, Any]],
    receipts: dict[int, dict[str, Any]],
    actual_state: ReplayState,
) -> list[dict[str, Any]]:
    expected_state, expected_receipts = derive_history(manifest, requests)
    if len(actual_state.events) > len(expected_state.events):
        raise RappError("history_mismatch", "event ledger is ahead of admitted history")
    if actual_state.events != expected_state.events[: len(actual_state.events)]:
        raise RappError("history_mismatch", "event ledger forks from admitted history")
    for issue_id, receipt in receipts.items():
        if expected_receipts.get(issue_id) != receipt:
            raise RappError(
                "history_mismatch",
                f"receipt for Issue database id {issue_id} is not reproducible",
            )

    for event in expected_state.events[len(actual_state.events) :]:
        write_json_immutable(
            root / "state" / "events" / event_filename(event["sequence"], event["hash"]),
            event,
        )
    write_json_atomic(root / "state" / "head.json", expected_state.head)

    created: list[dict[str, Any]] = []
    for issue_id, receipt in sorted(expected_receipts.items()):
        if issue_id in receipts:
            continue
        write_json_immutable(
            root / "state" / "receipts" / f"issue-{issue_id}.json",
            receipt,
        )
        created.append(receipt)
    return created


def _make_receipt_v1(
    request: dict[str, Any],
    *,
    status: str,
    code: str,
    message: str,
    event: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    duplicate_of: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = request["command"]
    receipt: dict[str, Any] = {
        "schema": "rapp-base-receipt/1.0",
        "actor_id": request["actor"]["id"],
        "code": code,
        "command_id": command["command_id"] if command is not None else None,
        "duplicate_of": duplicate_of,
        "event": (
            {
                "hash": event["hash"],
                "path": event_filename(event["sequence"], event["hash"]),
                "sequence": event["sequence"],
            }
            if event is not None
            else None
        ),
        "issue": copy.deepcopy(request["issue"]),
        "message": message,
        "occurred_at": request["admitted_at"],
        "receipt_id": ZERO_HASH,
        "record": (
            {
                "collection": record["collection"],
                "deleted": record["deleted"],
                "id": record["id"],
                "revision": record["revision"],
            }
            if record is not None
            else None
        ),
        "request_hash": request["request_hash"],
        "status": status,
    }
    receipt["receipt_id"] = object_hash(receipt, "receipt_id")
    return receipt
