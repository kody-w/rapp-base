"""Strict JSON, canonical serialization, paths, and timestamps."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import RappError

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_FORM_RE = re.compile(r"\A### Command\r?\n\r?\n(?P<command>.*)\Z", re.DOTALL)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_bytes(value: Any) -> bytes:
    """Return the only on-disk JSON representation used by the engine."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RappError("invalid_json_value", "value cannot be serialized safely") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_hash(value: Any, omitted_key: str | None = None) -> str:
    if omitted_key is not None and isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != omitted_key}
    return sha256_bytes(canonical_bytes(value))


def require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise RappError("invalid_hash", f"{field} must be a lowercase SHA-256")
    return value


def _reject_constant(value: str) -> None:
    raise RappError("invalid_number", f"{value} is not valid JSON")


def _parse_int(value: str) -> int:
    digits = value.lstrip("-")
    if len(digits) > 16:
        raise RappError("number_out_of_range", "integers must be JavaScript-safe")
    number = int(value)
    if abs(number) > 9_007_199_254_740_991:
        raise RappError("number_out_of_range", "integers must be JavaScript-safe")
    return number


def _parse_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RappError("invalid_number", "numbers must be finite")
    return number


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RappError("duplicate_key", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_tree(
    value: Any, limits: dict[str, int], *, allow_control_characters: bool
) -> None:
    nodes = 0
    keys = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes, keys
        nodes += 1
        if nodes > limits["json_nodes"]:
            raise RappError("too_many_nodes", "JSON value has too many nodes")
        if depth > limits["json_depth"]:
            raise RappError("too_deep", "JSON value is nested too deeply")
        if isinstance(item, dict):
            keys += len(item)
            if keys > limits["object_keys"]:
                raise RappError("too_many_keys", "JSON value has too many object keys")
            for key, child in item.items():
                _validate_string(
                    key, limits, allow_control_characters=allow_control_characters
                )
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > limits["array_items"]:
                raise RappError("array_too_large", "JSON array has too many items")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str):
            _validate_string(
                item, limits, allow_control_characters=allow_control_characters
            )
        elif isinstance(item, float) and not math.isfinite(item):
            raise RappError("invalid_number", "numbers must be finite")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise RappError("invalid_json_value", "unsupported JSON value")

    visit(value, 1)


def _validate_string(
    value: str, limits: dict[str, int], *, allow_control_characters: bool
) -> None:
    if not allow_control_characters and _CONTROL_RE.search(value):
        raise RappError("control_character", "control characters are not allowed")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise RappError("invalid_unicode", "strings must contain valid Unicode") from exc
    if size > limits["string_bytes"]:
        raise RappError("string_too_large", "JSON string exceeds the byte limit")


def strict_loads(
    text: str,
    limits: dict[str, int],
    *,
    byte_limit: int | None = None,
    require_object: bool = False,
    allow_control_characters: bool = False,
) -> Any:
    if not isinstance(text, str):
        raise RappError("invalid_json", "JSON input must be text")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RappError("invalid_unicode", "JSON must contain valid Unicode") from exc
    if byte_limit is not None and len(encoded) > byte_limit:
        raise RappError("command_too_large", "JSON command exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
            parse_float=_parse_float,
        )
    except RappError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RappError("invalid_json", "command must be exactly one JSON value") from exc
    if require_object and not isinstance(value, dict):
        raise RappError("not_an_object", "command must be a JSON object")
    _validate_tree(
        value, limits, allow_control_characters=allow_control_characters
    )
    return value


def extract_command_text(body: str, limits: dict[str, int]) -> str:
    if not isinstance(body, str):
        raise RappError("invalid_body", "issue body must be text")
    try:
        body_bytes = body.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RappError("invalid_unicode", "issue body must contain valid Unicode") from exc
    if len(body_bytes) > limits["issue_body_bytes"]:
        raise RappError("body_too_large", "issue body exceeds the byte limit")
    stripped = body.strip()
    if stripped.startswith("{"):
        candidate = stripped
    else:
        match = _FORM_RE.fullmatch(body.strip())
        if match is None:
            raise RappError(
                "invalid_issue_form",
                "body must contain only the Issue Form Command field",
            )
        candidate = match.group("command").strip()
    if not candidate:
        raise RappError("empty_command", "command is required")
    if candidate.startswith("```json\n") and candidate.endswith("\n```"):
        if candidate.count("```") != 2:
            raise RappError("invalid_issue_form", "exactly one JSON block is required")
        candidate = candidate[len("```json\n") : -len("\n```")].strip()
    elif "```" in candidate:
        raise RappError("invalid_issue_form", "Markdown code fences are not accepted")
    if len(candidate.encode("utf-8")) > limits["command_bytes"]:
        raise RappError("command_too_large", "JSON command exceeds the byte limit")
    return candidate


def load_json_file(
    path: Path,
    limits: dict[str, int],
    *,
    byte_limit: int | None = None,
    require_object: bool = True,
    allow_control_characters: bool = False,
) -> Any:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RappError("read_failed", f"cannot read {path}") from exc
    if byte_limit is not None and len(data) > byte_limit:
        raise RappError("file_too_large", f"{path} exceeds its byte limit")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RappError("invalid_unicode", f"{path} is not valid UTF-8") from exc
    return strict_loads(
        text,
        limits,
        byte_limit=byte_limit,
        require_object=require_object,
        allow_control_characters=allow_control_characters,
    )


def write_bytes_atomic(path: Path, data: bytes) -> bool:
    """Write only when bytes differ; leave no staging file behind."""

    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.new")
    try:
        with staging.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        if staging.exists():
            staging.unlink()
    return True


def write_json_atomic(path: Path, value: Any) -> bool:
    return write_bytes_atomic(path, canonical_bytes(value))


def write_json_immutable(path: Path, value: Any) -> bool:
    return write_bytes_immutable(path, canonical_bytes(value))


def require_canonical_json(path: Path, value: Any, context: str) -> None:
    if path.read_bytes() != canonical_bytes(value):
        raise RappError(
            "noncanonical_json",
            f"{context} is not encoded as canonical JSON: {path}",
        )


def write_bytes_immutable(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RappError("immutable_conflict", f"immutable file changed: {path}")
        return False
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise RappError("immutable_conflict", f"immutable file changed: {path}")
        return False
    return True


def normalize_timestamp(value: Any, field: str = "timestamp") -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RappError("invalid_timestamp", f"{field} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RappError("invalid_timestamp", f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RappError("invalid_timestamp", f"{field} must use UTC")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond:
        fraction = f"{parsed.microsecond:06d}".rstrip("0")
        return parsed.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_instant(value: Any, field: str = "timestamp") -> datetime:
    """Parse a canonicalizable RFC 3339 UTC timestamp for ordering."""

    normalized = normalize_timestamp(value, field)
    return datetime.fromisoformat(normalized[:-1] + "+00:00")


def latest_timestamp(*values: str) -> str:
    if not values:
        raise RappError("invalid_timestamp", "at least one timestamp is required")
    normalized = [normalize_timestamp(value) for value in values]
    return max(normalized, key=timestamp_instant)


def ensure_safe_segment(value: Any, field: str, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise RappError("invalid_path", f"{field} is not a safe lowercase URL segment")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise RappError("invalid_path", f"{field} is not a safe path segment")
    return value


def expect_keys(
    value: dict[str, Any],
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
    context: str,
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise RappError("missing_key", f"{context} is missing: {', '.join(missing)}")
    if unknown:
        raise RappError("unknown_key", f"{context} has unknown keys: {', '.join(unknown)}")
