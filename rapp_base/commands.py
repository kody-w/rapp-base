"""Issue provenance admission and strict command validation."""

from __future__ import annotations

import re
import uuid
from typing import Any

from .constants import (
    COLLECTION_PATTERN,
    COMMAND_SCHEMA,
    KNOWN_ASSOCIATIONS,
    PARSER_PROFILE,
    REQUEST_TITLE_PREFIX,
    SEGMENT_PATTERN,
    SYSTEM_FIELDS,
)
from .errors import RappError, public_error_message
from .jsonutil import (
    ensure_safe_segment,
    expect_keys,
    extract_command_text,
    normalize_timestamp,
    object_hash,
    require_hash,
    sha256_bytes,
    strict_loads,
)
from .manifest import collection_map, validate_actor_id

_NODE_RE = re.compile(r"^[A-Za-z0-9_=-]{1,200}$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def parse_command_text(text: str, limits: dict[str, int]) -> dict[str, Any]:
    command = strict_loads(
        text,
        limits,
        byte_limit=limits["command_bytes"],
        require_object=True,
    )
    expect_keys(
        command,
        required={"schema", "command_id", "operation", "collection"},
        optional={"record_id", "if_revision", "data"},
        context="command",
    )
    if command["schema"] != COMMAND_SCHEMA:
        raise RappError("invalid_command_schema", f"schema must be {COMMAND_SCHEMA}")
    command_id = command["command_id"]
    if not isinstance(command_id, str):
        raise RappError("invalid_command_id", "command_id must be a canonical UUID")
    try:
        parsed_uuid = uuid.UUID(command_id)
    except (ValueError, AttributeError) as exc:
        raise RappError("invalid_command_id", "command_id must be a canonical UUID") from exc
    if (
        str(parsed_uuid) != command_id
        or parsed_uuid.int == 0
        or parsed_uuid.variant != uuid.RFC_4122
        or parsed_uuid.version not in range(1, 9)
    ):
        raise RappError(
            "invalid_command_id", "command_id must be a canonical RFC UUID"
        )
    operation = command["operation"]
    if operation not in {"create", "update", "delete"}:
        raise RappError("invalid_operation", "operation must be create, update, or delete")
    ensure_safe_segment(command["collection"], "collection", COLLECTION_PATTERN)
    if operation == "create":
        if "record_id" in command or "if_revision" in command:
            raise RappError(
                "invalid_command_shape",
                "create forbids record_id and if_revision",
            )
        if "data" not in command:
            raise RappError("invalid_command_shape", "create requires data")
    elif operation == "update":
        if "record_id" not in command or "if_revision" not in command:
            raise RappError(
                "invalid_command_shape",
                "update requires record_id and if_revision",
            )
        if "data" not in command:
            raise RappError("invalid_command_shape", "update requires data")
    else:
        if "record_id" not in command or "if_revision" not in command:
            raise RappError(
                "invalid_command_shape",
                "delete requires record_id and if_revision",
            )
        if "data" in command:
            raise RappError("invalid_command_shape", "delete forbids data")
    if "record_id" in command:
        ensure_safe_segment(command["record_id"], "record_id", SEGMENT_PATTERN)
    if "if_revision" in command:
        require_hash(command["if_revision"], "if_revision")
    if "data" in command:
        if not isinstance(command["data"], dict):
            raise RappError("invalid_data", "data must be a JSON object")
        reserved = sorted(command["data"].keys() & SYSTEM_FIELDS)
        if reserved:
            raise RappError(
                "reserved_field", f"data contains reserved fields: {', '.join(reserved)}"
            )
    return command


def normalize_repository(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RappError("invalid_provenance", "repository metadata must be an object")
    expect_keys(
        value,
        required={"id", "node_id", "full_name"},
        context="repository metadata",
    )
    repository_id = value["id"]
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id < 1
        or repository_id > 9_007_199_254_740_991
    ):
        raise RappError("invalid_provenance", "repository database id is invalid")
    if not isinstance(value["node_id"], str) or _NODE_RE.fullmatch(value["node_id"]) is None:
        raise RappError("invalid_provenance", "repository node id is invalid")
    if (
        not isinstance(value["full_name"], str)
        or _FULL_NAME_RE.fullmatch(value["full_name"]) is None
    ):
        raise RappError("invalid_provenance", "repository full name is invalid")
    if any(part in {".", ".."} for part in value["full_name"].split("/")):
        raise RappError("invalid_provenance", "repository full name is invalid")
    return {
        "full_name": value["full_name"],
        "id": repository_id,
        "node_id": value["node_id"],
    }


def normalize_issue(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RappError("invalid_provenance", "issue metadata must be an object")
    expect_keys(
        value,
        required={
            "id",
            "node_id",
            "number",
            "created_at",
            "updated_at",
            "title",
            "user",
            "author_association",
            "labels",
            "body",
            "state",
        },
        context="issue metadata",
    )
    for key in ("id", "number"):
        item = value[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 1
            or item > 9_007_199_254_740_991
        ):
            raise RappError("invalid_provenance", f"issue {key} is invalid")
    if (
        not isinstance(value["node_id"], str)
        or _NODE_RE.fullmatch(value["node_id"]) is None
    ):
        raise RappError("invalid_provenance", "issue node id is invalid")
    if not isinstance(value["user"], dict):
        raise RappError("invalid_provenance", "issue user metadata is invalid")
    expect_keys(value["user"], required={"id"}, context="issue user metadata")
    actor_id = validate_actor_id(value["user"]["id"])
    association = value["author_association"]
    if association not in KNOWN_ASSOCIATIONS:
        raise RappError("invalid_provenance", "issue author association is unknown")
    labels = value["labels"]
    if (
        not isinstance(labels, list)
        or len(labels) > 100
        or not all(isinstance(label, str) for label in labels)
    ):
        raise RappError("invalid_provenance", "issue labels are invalid")
    if not isinstance(value["body"], str):
        raise RappError("invalid_provenance", "issue body is invalid")
    if not isinstance(value["title"], str) or not value["title"]:
        raise RappError("invalid_provenance", "issue title is invalid")
    try:
        title_bytes = value["title"].encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RappError("invalid_provenance", "issue title is invalid") from exc
    if len(title_bytes) > 1024 or any(
        ord(character) < 32 for character in value["title"]
    ):
        raise RappError("invalid_provenance", "issue title is invalid")
    if value["state"] not in {"open", "closed"}:
        raise RappError("invalid_provenance", "issue state is invalid")
    return {
        "author_association": association,
        "body": value["body"],
        "created_at": normalize_timestamp(value["created_at"], "issue created_at"),
        "id": value["id"],
        "labels": sorted(set(labels)),
        "node_id": value["node_id"],
        "number": value["number"],
        "state": value["state"],
        "title": value["title"],
        "updated_at": normalize_timestamp(value["updated_at"], "issue updated_at"),
        "user": {"id": actor_id},
    }


def is_request_issue(issue: dict[str, Any]) -> bool:
    return issue["state"] == "open" and issue["title"].startswith(
        REQUEST_TITLE_PREFIX
    )


def admit_request(
    repository: dict[str, Any],
    issue: dict[str, Any],
    manifest: dict[str, Any],
    *,
    admission_sequence: int,
) -> dict[str, Any]:
    limits = manifest["limits"]
    body_bytes = issue["body"].encode("utf-8", errors="strict")
    command: dict[str, Any] | None = None
    command_text: str | None = None
    command_sha256: str | None = None
    parse_error: dict[str, str] | None = None
    try:
        command_text = extract_command_text(issue["body"], limits)
        command_sha256 = sha256_bytes(command_text.encode("utf-8"))
        command = parse_command_text(command_text, limits)
    except RappError as exc:
        parse_error = {
            "code": exc.code,
            "message": public_error_message(exc.code),
        }
    policy = None
    if command is not None:
        collection = collection_map(manifest).get(command["collection"])
        if collection is not None:
            policy = collection["policies"][command["operation"]]
    envelope: dict[str, Any] = {
        "schema": "rapp-base-request/1.0",
        "admission": {
            "limits": {key: limits[key] for key in sorted(limits)},
            "parser_profile": PARSER_PROFILE,
            "policy": policy,
        },
        "actor": {
            "association": issue["author_association"],
            "id": issue["user"]["id"],
        },
        "admission_sequence": admission_sequence,
        "admitted_at": issue["created_at"],
        "body_sha256": sha256_bytes(body_bytes),
        "command": command,
        "command_sha256": command_sha256,
        "command_text": command_text,
        "issue": {
            "id": issue["id"],
            "node_id": issue["node_id"],
            "number": issue["number"],
            "title": issue["title"],
        },
        "parse_error": parse_error,
        "repository": repository,
    }
    envelope["request_hash"] = object_hash(envelope)
    return envelope


def request_filename(issue_id: int) -> str:
    return f"issue-{issue_id}.json"
