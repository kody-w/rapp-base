from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rapp_base.jsonutil import canonical_bytes
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import reconcile_document
from rapp_base.state import head_for_events

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = {
    "full_name": "kody-w/rapp-base",
    "id": 9001,
    "node_id": "R_repo9001",
}


@contextmanager
def repository():
    scratch_root = PROJECT_ROOT / ".test-work"
    root = scratch_root / f"case-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "manifest.json", root / "manifest.json")
    for relative in (
        "state/requests",
        "state/receipts",
        "state/events",
        "api/v1",
        "versions",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "state" / "head.json").write_bytes(
        canonical_bytes(head_for_events(load_manifest(root), []))
    )
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            scratch_root.rmdir()
        except OSError:
            pass


def command_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


def resource_data(
    number: int,
    *,
    title: str | None = None,
    rating: int | float = 4,
) -> dict:
    return {
        "title": title or f"Resource {number}",
        "url": f"https://example.com/resource/{number}",
        "kind": "article",
        "summary": f"A useful public resource number {number}.",
        "topics": ["example", f"topic-{number}"],
        "free": True,
        "rating": rating,
    }


def create_command(number: int, *, command_uuid: str | None = None, data=None) -> dict:
    return {
        "schema": "rapp-base-command/1.0",
        "command_id": command_uuid or command_id(number),
        "operation": "create",
        "collection": "resources",
        "data": data if data is not None else resource_data(number),
    }


def issue(
    number: int,
    command: dict | str,
    *,
    actor_id: int = 1001,
    association: str = "NONE",
    issue_id: int | None = None,
    fenced: bool = True,
    labels: list[str] | None = None,
    title: str = "[RAPP Base] command",
) -> dict:
    text = (
        command
        if isinstance(command, str)
        else json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    )
    if fenced:
        body = f"### Command\n\n```json\n{text}\n```"
    else:
        body = text
    timestamp = datetime(2026, 7, 18, 19, 0, tzinfo=timezone.utc) + timedelta(
        seconds=number
    )
    rendered = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    database_id = issue_id or 100_000 + number
    return {
        "author_association": association,
        "body": body,
        "created_at": rendered,
        "id": database_id,
        "labels": labels if labels is not None else [],
        "node_id": f"I_issue{database_id}",
        "number": number,
        "state": "open",
        "title": title,
        "updated_at": rendered,
        "user": {"id": actor_id},
    }


def reconcile(root: Path, issues: list[dict]):
    return reconcile_document(
        root,
        load_manifest(root),
        {"repository": REPOSITORY, "issues": issues},
    )


def load_receipt(root: Path, issue_value: dict) -> dict:
    path = root / "state" / "receipts" / f"issue-{issue_value['id']}.json"
    return json.loads(path.read_text(encoding="utf-8"))
