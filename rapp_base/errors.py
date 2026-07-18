"""Stable engine errors."""


class RappError(ValueError):
    """An expected validation or state transition failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_PUBLIC_MESSAGES = {
    "array_too_large": "a JSON array exceeds the item limit",
    "body_too_large": "the Issue body exceeds the byte limit",
    "command_id_conflict": "command_id was already admitted with different bytes",
    "command_too_large": "the JSON command exceeds the byte limit",
    "control_character": "control characters are not allowed",
    "duplicate_array_item": "string arrays cannot contain duplicate items",
    "duplicate_key": "duplicate JSON object keys are not allowed",
    "empty_command": "a JSON command is required",
    "enum": "a field value is outside its enum",
    "event_limit": "the applied event limit has been reached",
    "field_type": "a field value has the wrong type",
    "forbidden": "the GitHub actor is not authorized for this operation",
    "invalid_command_id": "command_id must be a canonical RFC UUID",
    "invalid_command_schema": "the command schema is not supported",
    "invalid_command_shape": "the command keys do not match its operation",
    "invalid_data": "data must be a JSON object",
    "invalid_body": "the Issue body is invalid",
    "invalid_hash": "if_revision must be a full lowercase SHA-256",
    "invalid_issue_form": "the Issue must contain only the Command form field",
    "invalid_json": "the command must be exactly one valid JSON object",
    "invalid_json_value": "the command contains an unsupported JSON value",
    "invalid_number": "JSON numbers must be finite",
    "invalid_operation": "operation must be create, update, or delete",
    "invalid_path": "a collection, field, or record path is unsafe",
    "invalid_unicode": "the Issue must contain valid Unicode",
    "max_length": "a field exceeds its maximum length",
    "maximum": "a numeric field exceeds its maximum",
    "min_length": "a field is shorter than its minimum length",
    "minimum": "a numeric field is below its minimum",
    "missing_key": "the command is missing a required key",
    "no_change": "the update does not change record data",
    "not_an_object": "the command must be a JSON object",
    "not_found": "the record does not exist or is deleted",
    "number_out_of_range": "a number is outside the supported safe range",
    "policy_disabled": "this operation is disabled",
    "record_exists": "the deterministic record id already exists",
    "record_limit": "the collection record limit has been reached",
    "request_limit": "the admitted request limit has been reached",
    "required_field": "data is missing a required field",
    "reserved_field": "data contains a reserved system field",
    "stale_revision": "if_revision does not match the current record",
    "string_too_large": "a JSON string exceeds the byte limit",
    "too_deep": "the JSON command is nested too deeply",
    "too_many_keys": "the JSON command has too many object keys",
    "too_many_nodes": "the JSON command has too many nodes",
    "unique": "a unique field value is already in use",
    "unknown_collection": "the collection does not exist",
    "unknown_field": "data contains a field outside the collection schema",
    "unknown_key": "the command contains an unknown key",
    "url_format": "a URL field must be an absolute HTTPS URL",
}


def public_error_message(code: str) -> str:
    """Return a bounded message that never echoes untrusted Issue text."""

    return _PUBLIC_MESSAGES.get(code, "the command was rejected by the RAPP Base engine")


def is_command_rejection(code: str) -> bool:
    return code in _PUBLIC_MESSAGES
