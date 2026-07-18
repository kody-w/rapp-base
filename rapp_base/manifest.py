"""Manifest validation, field validation, uniqueness, and authorization."""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import (
    COLLABORATOR_ASSOCIATIONS,
    COLLECTION_PATTERN,
    HARD_LIMITS,
    KNOWN_ASSOCIATIONS,
    LIMIT_KEYS,
    MAINTAINER_ASSOCIATIONS,
    MANIFEST_SCHEMA,
    POLICIES_CREATE,
    POLICIES_MUTATE,
    PROFILE,
    SEGMENT_PATTERN,
    SYSTEM_FIELDS,
)
from .errors import RappError
from .jsonutil import (
    ensure_safe_segment,
    expect_keys,
    load_json_file,
    normalize_timestamp,
    object_hash,
)

_FIELD_TYPES = frozenset({"string", "number", "integer", "boolean", "string[]"})
_FIELD_KEYS = frozenset(
    {
        "type",
        "required",
        "enum",
        "min",
        "max",
        "minLength",
        "maxLength",
        "format",
        "unique",
        "description",
    }
)
_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    manifest = load_json_file(
        path,
        {
            **HARD_LIMITS,
            "array_items": HARD_LIMITS["records_per_collection"],
            "json_nodes": 100_000,
            "object_keys": 50_000,
        },
        byte_limit=1_048_576,
    )
    expect_keys(
        manifest,
        required={
            "schema",
            "name",
            "profile",
            "description",
            "repository",
            "generated_at",
            "limits",
            "collections",
        },
        context="manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise RappError("invalid_manifest", f"manifest must use {MANIFEST_SCHEMA}")
    if manifest["profile"] != PROFILE:
        raise RappError("invalid_manifest", f"manifest profile must be {PROFILE}")
    for field in ("name", "description"):
        if not isinstance(manifest[field], str) or not manifest[field].strip():
            raise RappError("invalid_manifest", f"manifest {field} must be non-empty text")
    manifest["generated_at"] = normalize_timestamp(
        manifest["generated_at"], "manifest generated_at"
    )
    _validate_repository(manifest["repository"])
    _validate_limits(manifest["limits"])
    collections = manifest["collections"]
    if not isinstance(collections, list) or not collections:
        raise RappError("invalid_manifest", "manifest collections must be a non-empty array")
    if len(collections) > 64:
        raise RappError("invalid_manifest", "manifest has too many collections")
    names: set[str] = set()
    for collection in collections:
        _validate_collection(collection, manifest["limits"])
        if collection["name"] in names:
            raise RappError("invalid_manifest", "collection names must be unique")
        names.add(collection["name"])
    return manifest


def genesis_sha256(manifest: dict[str, Any]) -> str:
    """Hash only inputs that can change genesis records or event interpretation."""

    collections = []
    for collection in sorted(manifest["collections"], key=lambda item: item["name"]):
        fields = {
            name: {
                key: copy.deepcopy(value)
                for key, value in sorted(spec.items())
                if key != "description"
            }
            for name, spec in sorted(collection["fields"].items())
        }
        seeds = sorted(
            (copy.deepcopy(seed) for seed in collection["seed"]),
            key=lambda seed: seed["id"],
        )
        collections.append(
            {
                "fields": fields,
                "name": collection["name"],
                "seed": seeds,
            }
        )
    return object_hash(
        {
            "collections": collections,
            "schema": "rapp-base-genesis/1.0",
        }
    )


def _validate_repository(value: Any) -> None:
    if not isinstance(value, dict):
        raise RappError("invalid_manifest", "repository must be an object")
    expect_keys(
        value,
        required={"owner", "name", "branch"},
        context="manifest repository",
    )
    if _GITHUB_NAME_RE.fullmatch(value["owner"]) is None:
        raise RappError("invalid_manifest", "repository owner is invalid")
    if _GITHUB_NAME_RE.fullmatch(value["name"]) is None:
        raise RappError("invalid_manifest", "repository name is invalid")
    if value["owner"] in {".", ".."} or value["name"] in {".", ".."}:
        raise RappError("invalid_manifest", "repository path segments are invalid")
    if value["branch"] != "main":
        raise RappError("invalid_manifest", "repository branch must be main")


def _validate_limits(value: Any) -> None:
    if not isinstance(value, dict):
        raise RappError("invalid_manifest", "limits must be an object")
    if set(value) != LIMIT_KEYS:
        missing = sorted(LIMIT_KEYS - value.keys())
        unknown = sorted(value.keys() - LIMIT_KEYS)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise RappError("invalid_manifest", "invalid limits: " + "; ".join(details))
    for key, ceiling in HARD_LIMITS.items():
        current = value[key]
        if isinstance(current, bool) or not isinstance(current, int) or current < 1:
            raise RappError("invalid_manifest", f"limit {key} must be a positive integer")
        if current > ceiling:
            raise RappError(
                "invalid_manifest", f"limit {key} exceeds hard ceiling {ceiling}"
            )
    if value["snapshot_items"] > value["records_per_collection"]:
        raise RappError(
            "invalid_manifest", "snapshot_items cannot exceed records_per_collection"
        )


def _validate_collection(value: Any, limits: dict[str, int]) -> None:
    if not isinstance(value, dict):
        raise RappError("invalid_manifest", "each collection must be an object")
    expect_keys(
        value,
        required={"name", "description", "policies", "fields", "seed"},
        context="collection",
    )
    ensure_safe_segment(value["name"], "collection name", COLLECTION_PATTERN)
    if not isinstance(value["description"], str) or not value["description"].strip():
        raise RappError("invalid_manifest", "collection description must be non-empty")
    _validate_policies(value["policies"])
    fields = value["fields"]
    if not isinstance(fields, dict) or not fields:
        raise RappError("invalid_manifest", "collection fields must be a non-empty object")
    if len(fields) > limits["fields_per_collection"]:
        raise RappError("invalid_manifest", "collection has too many fields")
    for field_name, field in fields.items():
        ensure_safe_segment(field_name, "field name", SEGMENT_PATTERN)
        if field_name in SYSTEM_FIELDS:
            raise RappError("reserved_field", f"{field_name} is reserved")
        _validate_field(field_name, field)
    seed = value["seed"]
    if not isinstance(seed, list):
        raise RappError("invalid_manifest", "collection seed must be an array")
    if len(seed) > limits["records_per_collection"]:
        raise RappError("invalid_manifest", "seed exceeds the collection record limit")
    seen_ids: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    for item in seed:
        if not isinstance(item, dict):
            raise RappError("invalid_manifest", "seed entries must be objects")
        expect_keys(
            item,
            required={"id", "owner_id", "created_at", "data"},
            context="seed record",
        )
        record_id = ensure_safe_segment(item["id"], "seed id", SEGMENT_PATTERN)
        if record_id in seen_ids:
            raise RappError("invalid_manifest", "seed record ids must be unique")
        seen_ids.add(record_id)
        _validate_actor_id(item["owner_id"], "seed owner_id", allow_zero=True)
        item["created_at"] = normalize_timestamp(
            item["created_at"], "seed created_at"
        )
        normalized = validate_data(value, item["data"])
        assert_unique(value, records, normalized)
        records[record_id] = {"data": normalized, "deleted": False}


def _validate_policies(value: Any) -> None:
    if not isinstance(value, dict):
        raise RappError("invalid_manifest", "policies must be an object")
    expect_keys(
        value,
        required={"create", "update", "delete"},
        context="collection policies",
    )
    if value["create"] not in POLICIES_CREATE:
        raise RappError("invalid_manifest", "invalid create policy")
    for operation in ("update", "delete"):
        if value[operation] not in POLICIES_MUTATE:
            raise RappError("invalid_manifest", f"invalid {operation} policy")


def _validate_field(name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise RappError("invalid_manifest", f"field {name} must be an object")
    if "type" not in value:
        raise RappError("invalid_manifest", f"field {name} requires type")
    unknown = value.keys() - _FIELD_KEYS
    if unknown:
        raise RappError(
            "invalid_manifest",
            f"field {name} has unknown keys: {', '.join(sorted(unknown))}",
        )
    field_type = value["type"]
    if field_type not in _FIELD_TYPES:
        raise RappError("invalid_manifest", f"field {name} has invalid type")
    for flag in ("required", "unique"):
        if flag in value and not isinstance(value[flag], bool):
            raise RappError("invalid_manifest", f"field {name} {flag} must be boolean")
    if value.get("unique") and field_type == "string[]":
        raise RappError("invalid_manifest", f"field {name} arrays cannot be unique")
    if "description" in value and not isinstance(value["description"], str):
        raise RappError("invalid_manifest", f"field {name} description must be text")
    for bound in ("minLength", "maxLength"):
        if bound in value and (
            isinstance(value[bound], bool)
            or not isinstance(value[bound], int)
            or value[bound] < 0
        ):
            raise RappError("invalid_manifest", f"field {name} {bound} is invalid")
    if "minLength" in value and "maxLength" in value:
        if value["minLength"] > value["maxLength"]:
            raise RappError("invalid_manifest", f"field {name} length bounds are invalid")
    for bound in ("min", "max"):
        if bound in value and (
            isinstance(value[bound], bool)
            or not isinstance(value[bound], (int, float))
            or not math.isfinite(value[bound])
        ):
            raise RappError("invalid_manifest", f"field {name} {bound} is invalid")
    if any(bound in value for bound in ("min", "max")) and field_type not in {
        "number",
        "integer",
    }:
        raise RappError("invalid_manifest", f"field {name} numeric bounds need a number")
    if "min" in value and "max" in value and value["min"] > value["max"]:
        raise RappError("invalid_manifest", f"field {name} numeric bounds are invalid")
    if any(bound in value for bound in ("minLength", "maxLength")) and field_type not in {
        "string",
        "string[]",
    }:
        raise RappError("invalid_manifest", f"field {name} length bounds are invalid")
    if "format" in value and (field_type != "string" or value["format"] != "url"):
        raise RappError("invalid_manifest", f"field {name} has unsupported format")
    if "enum" in value:
        if field_type == "string[]":
            raise RappError(
                "invalid_manifest", f"field {name} arrays do not support enum"
            )
        enum = value["enum"]
        if not isinstance(enum, list) or not enum or len(enum) > 100:
            raise RappError("invalid_manifest", f"field {name} enum is invalid")
        canonical: set[tuple[type[Any], Any]] = set()
        for item in enum:
            _validate_field_type(name, field_type, item)
            marker = (type(item), item)
            if marker in canonical:
                raise RappError("invalid_manifest", f"field {name} enum has duplicates")
            canonical.add(marker)


def collection_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {collection["name"]: collection for collection in manifest["collections"]}


def validate_data(
    collection: dict[str, Any],
    data: Any,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RappError("invalid_data", "data must be a JSON object")
    fields = collection["fields"]
    unknown = sorted(data.keys() - fields.keys())
    if unknown:
        raise RappError("unknown_field", f"unknown data fields: {', '.join(unknown)}")
    missing = sorted(
        name
        for name, spec in fields.items()
        if spec.get("required", False) and name not in data
    )
    if missing:
        raise RappError("required_field", f"required data fields: {', '.join(missing)}")
    normalized: dict[str, Any] = {}
    for name in sorted(data):
        value = data[name]
        spec = fields[name]
        _validate_field_type(name, spec["type"], value)
        if "enum" in spec and value not in spec["enum"]:
            raise RappError("enum", f"{name} is not an allowed value")
        if spec["type"] in {"string", "string[]"}:
            length = len(value)
            if "minLength" in spec and length < spec["minLength"]:
                raise RappError("min_length", f"{name} is too short")
            if "maxLength" in spec and length > spec["maxLength"]:
                raise RappError("max_length", f"{name} is too long")
        if spec["type"] in {"number", "integer"}:
            if "min" in spec and value < spec["min"]:
                raise RappError("minimum", f"{name} is below its minimum")
            if "max" in spec and value > spec["max"]:
                raise RappError("maximum", f"{name} exceeds its maximum")
        if spec.get("format") == "url":
            _validate_url(name, value)
        normalized[name] = value
    return normalized


def _validate_field_type(name: str, field_type: str, value: Any) -> None:
    valid = False
    if field_type == "string":
        valid = isinstance(value, str)
    elif field_type == "number":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        )
    elif field_type == "integer":
        valid = not isinstance(value, bool) and isinstance(value, int)
    elif field_type == "boolean":
        valid = isinstance(value, bool)
    elif field_type == "string[]":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        if valid and len(set(value)) != len(value):
            raise RappError("duplicate_array_item", f"{name} contains duplicates")
    if not valid:
        raise RappError("field_type", f"{name} must be {field_type}")


def _validate_url(name: str, value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RappError("url_format", f"{name} must be an absolute HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise RappError("url_format", f"{name} must be an absolute HTTPS URL")


def assert_unique(
    collection: dict[str, Any],
    records: dict[str, dict[str, Any]],
    data: dict[str, Any],
    *,
    exclude_id: str | None = None,
) -> None:
    unique_fields = [
        name for name, spec in collection["fields"].items() if spec.get("unique")
    ]
    for record_id, record in records.items():
        if record_id == exclude_id or record.get("deleted"):
            continue
        existing = record["data"]
        for field in unique_fields:
            if field in data and field in existing and data[field] == existing[field]:
                raise RappError("unique", f"{field} must be unique")


def authorize(
    policy: str,
    *,
    actor_id: int,
    association: str,
    owner_id: int | None = None,
) -> str:
    _validate_actor_id(actor_id, "actor id")
    if association not in KNOWN_ASSOCIATIONS:
        raise RappError("invalid_identity", "unknown GitHub author association")
    if policy == "disabled":
        raise RappError("policy_disabled", "this operation is disabled")
    if policy == "public":
        return "public"
    if policy == "collaborator" and association in COLLABORATOR_ASSOCIATIONS:
        return "collaborator"
    if policy == "maintainer" and association in MAINTAINER_ASSOCIATIONS:
        return "maintainer"
    if policy == "owner" and (actor_id == owner_id or association == "OWNER"):
        return "owner" if actor_id == owner_id else "repository_owner"
    raise RappError("forbidden", "the GitHub actor is not authorized")


def _validate_actor_id(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > 9_007_199_254_740_991
    ):
        raise RappError("invalid_identity", f"{field} must be a numeric GitHub user id")
    return value


def validate_actor_id(value: Any) -> int:
    return _validate_actor_id(value, "actor id")
