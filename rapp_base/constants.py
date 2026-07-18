"""Protocol constants and hard safety ceilings."""

COMMAND_SCHEMA = "rapp-base-command/1.0"
MANIFEST_SCHEMA = "rapp-base-manifest/1.0"
PROFILE = "rapp-base/1.0"
BASE_SCHEMA = "rapp-static-api/1.0"
BUILDER_PROFILE = "rapp-base-builder/1.0"
PARSER_PROFILE = "rapp-base-command-parser/1.0"
REQUEST_LABEL = "rapp-base-request"
REQUEST_TITLE_PREFIX = "[RAPP Base]"

API_MAJOR = 1
API_PREFIX = f"api/v{API_MAJOR}"

ZERO_HASH = "0" * 64
SEGMENT_PATTERN = r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$"
COLLECTION_PATTERN = r"^[a-z][a-z0-9_-]{0,62}$"

POLICIES_CREATE = frozenset({"public", "collaborator", "maintainer", "disabled"})
POLICIES_MUTATE = frozenset(
    {"owner", "public", "collaborator", "maintainer", "disabled"}
)
COLLABORATOR_ASSOCIATIONS = frozenset({"OWNER", "COLLABORATOR"})
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER"})
KNOWN_ASSOCIATIONS = frozenset(
    {
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
        "CONTRIBUTOR",
        "FIRST_TIMER",
        "FIRST_TIME_CONTRIBUTOR",
        "MANNEQUIN",
        "NONE",
    }
)

LIMIT_KEYS = frozenset(
    {
        "issue_body_bytes",
        "command_bytes",
        "json_depth",
        "json_nodes",
        "object_keys",
        "array_items",
        "string_bytes",
        "fields_per_collection",
        "records_per_collection",
        "snapshot_items",
        "generated_collection_bytes",
        "issues_per_reconcile",
        "requests",
        "events",
    }
)

HARD_LIMITS = {
    "issue_body_bytes": 65536,
    "command_bytes": 32768,
    "json_depth": 12,
    "json_nodes": 2048,
    "object_keys": 256,
    "array_items": 256,
    "string_bytes": 8192,
    "fields_per_collection": 64,
    "records_per_collection": 2000,
    "snapshot_items": 2000,
    "generated_collection_bytes": 8388608,
    "issues_per_reconcile": 1000,
    "requests": 50000,
    "events": 50000,
}

PARSER_LIMIT_KEYS = frozenset(
    {
        "issue_body_bytes",
        "command_bytes",
        "json_depth",
        "json_nodes",
        "object_keys",
        "array_items",
        "string_bytes",
    }
)

# Legacy valid v1 request envelopes may contain raw command text. Keep those
# immutable snapshots loadable while new admissions retain hash-only text.
REQUEST_ENVELOPE_LIMITS = {
    **HARD_LIMITS,
    "json_depth": HARD_LIMITS["json_depth"] + 8,
    "json_nodes": HARD_LIMITS["json_nodes"] + 1024,
    "object_keys": HARD_LIMITS["object_keys"] + 256,
    "array_items": HARD_LIMITS["array_items"] + 64,
    "string_bytes": HARD_LIMITS["command_bytes"],
}
REQUEST_ENVELOPE_BYTES = (
    HARD_LIMITS["issue_body_bytes"] + HARD_LIMITS["command_bytes"] + 262_144
)

SYSTEM_FIELDS = frozenset(
    {
        "$schema",
        "schema",
        "id",
        "collection",
        "owner_id",
        "created_at",
        "updated_at",
        "deleted_at",
        "deleted",
        "prior_revision",
        "revision",
        "__proto__",
        "prototype",
        "constructor",
    }
)
