from __future__ import annotations

import json
import unittest
from pathlib import Path

from rapp_base.build import build
from rapp_base.errors import RappError
from rapp_base.jsonutil import canonical_bytes
from rapp_base.manifest import load_manifest
from rapp_base.reconcile import load_receipts, load_requests
from rapp_base.state import deterministic_record_id, replay

from helpers import (
    PROJECT_ROOT,
    command_id,
    create_command,
    issue,
    load_receipt,
    reconcile,
    repository,
    resource_data,
)


class EngineTests(unittest.TestCase):
    def test_programmatic_raw_json_body_remains_accepted(self):
        with repository() as root:
            submitted = issue(1, create_command(1), fenced=False)
            reconcile(root, [submitted])
            self.assertEqual(load_receipt(root, submitted)["status"], "applied")

    def test_committed_v1_sdk_body_shape_remains_accepted(self):
        with repository() as root:
            command = create_command(2)
            text = json.dumps(command, ensure_ascii=False, indent=2)
            submitted = issue(2, command, fenced=False)
            submitted["body"] = f"### Command\n\n```json\n{text}\n```"
            reconcile(root, [submitted])
            self.assertEqual(load_receipt(root, submitted)["status"], "applied")

    def test_existing_immutable_v1_requests_still_replay(self):
        manifest = load_manifest(PROJECT_ROOT)
        requests = load_requests(PROJECT_ROOT, manifest)
        self.assertEqual(len(requests), 3)
        self.assertTrue(
            all(isinstance(request["command_text"], str) for request in requests.values())
        )
        summary = build(PROJECT_ROOT, manifest, write=False)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["events"], 3)

    def test_deterministic_create_ids_are_stable_and_identity_bound(self):
        values = {
            "repository_id": 1,
            "issue_id": 2,
            "issue_node_id": "I_node",
            "command_id": command_id(1),
            "collection": "resources",
        }
        first = deterministic_record_id(**values)
        self.assertEqual(first, deterministic_record_id(**values))
        self.assertRegex(first, r"^r_[0-9a-f]{24}$")
        values["issue_id"] = 3
        self.assertNotEqual(first, deterministic_record_id(**values))

    def test_identity_spoofing_is_rejected_and_owner_comes_from_metadata(self):
        with repository() as root:
            spoofed = create_command(1)
            spoofed["data"]["owner_id"] = 999999
            bad_issue = issue(1, spoofed, actor_id=44)
            good_issue = issue(2, create_command(2), actor_id=44)
            reconcile(root, [bad_issue, good_issue])
            self.assertEqual(load_receipt(root, bad_issue)["code"], "reserved_field")
            good_receipt = load_receipt(root, good_issue)
            state = replay(root, load_manifest(root))
            record = state.records["resources"][good_receipt["record"]["id"]]
            self.assertEqual(record["owner_id"], 44)
            self.assertEqual(len(state.events), 1)

    def test_create_update_stale_foreign_update_and_delete(self):
        with repository() as root:
            created_issue = issue(1, create_command(1), actor_id=101)
            reconcile(root, [created_issue])
            created = load_receipt(root, created_issue)
            record_id = created["record"]["id"]
            first_revision = created["record"]["revision"]

            update = {
                "schema": "rapp-base-command/1.0",
                "command_id": command_id(2),
                "operation": "update",
                "collection": "resources",
                "record_id": record_id,
                "if_revision": first_revision,
                "data": {"rating": 5},
            }
            update_issue = issue(2, update, actor_id=101)
            reconcile(root, [update_issue])
            updated = load_receipt(root, update_issue)
            self.assertEqual(updated["status"], "applied")
            second_revision = updated["record"]["revision"]
            self.assertNotEqual(first_revision, second_revision)

            stale = dict(update)
            stale["command_id"] = command_id(3)
            stale_issue = issue(3, stale, actor_id=101)
            reconcile(root, [stale_issue])
            self.assertEqual(load_receipt(root, stale_issue)["code"], "stale_revision")

            foreign = dict(update)
            foreign["command_id"] = command_id(4)
            foreign["if_revision"] = second_revision
            foreign_issue = issue(4, foreign, actor_id=202)
            reconcile(root, [foreign_issue])
            self.assertEqual(load_receipt(root, foreign_issue)["code"], "forbidden")

            delete = {
                "schema": "rapp-base-command/1.0",
                "command_id": command_id(5),
                "operation": "delete",
                "collection": "resources",
                "record_id": record_id,
                "if_revision": second_revision,
            }
            delete_issue = issue(5, delete, actor_id=101)
            reconcile(root, [delete_issue])
            removed = load_receipt(root, delete_issue)
            self.assertEqual(removed["code"], "deleted")
            build(root, load_manifest(root))
            current = json.loads(
                (
                    root
                    / "api/v1/collections/resources/records"
                    / f"{record_id}.json"
                ).read_text()
            )
            listing = json.loads(
                (root / "api/v1/collections/resources/records.json").read_text()
            )
            self.assertTrue(current["deleted"])
            self.assertNotIn(record_id, [item["id"] for item in listing["items"]])

    def test_identical_replay_is_noop_and_changed_bytes_conflict(self):
        with repository() as root:
            shared_id = command_id(10)
            first_command = create_command(10, command_uuid=shared_id)
            first = issue(10, first_command)
            identical = issue(11, first_command)
            changed_command = create_command(
                10,
                command_uuid=shared_id,
                data={**resource_data(10), "rating": 5},
            )
            changed = issue(12, changed_command)
            reconcile(root, [changed, identical, first])
            self.assertEqual(load_receipt(root, first)["status"], "applied")
            self.assertEqual(load_receipt(root, identical)["code"], "identical_replay")
            self.assertEqual(load_receipt(root, changed)["code"], "command_id_conflict")
            self.assertEqual(len(replay(root, load_manifest(root)).events), 1)

    def test_value_equivalent_update_is_rejected_without_advancing_revision(self):
        with repository() as root:
            created_issue = issue(13, create_command(13), actor_id=1313)
            reconcile(root, [created_issue])
            created = load_receipt(root, created_issue)
            update = {
                "schema": "rapp-base-command/1.0",
                "command_id": command_id(14),
                "operation": "update",
                "collection": "resources",
                "record_id": created["record"]["id"],
                "if_revision": created["record"]["revision"],
                "data": {"rating": 4},
            }
            update_issue = issue(14, update, actor_id=1313)
            reconcile(root, [update_issue])
            self.assertEqual(load_receipt(root, update_issue)["code"], "no_change")
            state = replay(root, load_manifest(root))
            self.assertEqual(len(state.events), 1)
            self.assertEqual(
                state.records["resources"][created["record"]["id"]]["revision"],
                created["record"]["revision"],
            )

    def test_issue_edit_does_not_retry_first_admission(self):
        with repository() as root:
            malformed = issue(20, "{not json", issue_id=555)
            reconcile(root, [malformed])
            first = load_receipt(root, malformed)
            edited = issue(20, create_command(20), issue_id=555)
            edited["updated_at"] = "2026-07-19T00:00:00Z"
            summary = reconcile(root, [edited])
            second = load_receipt(root, edited)
            self.assertEqual(first, second)
            self.assertEqual(summary["existing"], 1)
            self.assertEqual(len(replay(root, load_manifest(root)).events), 0)

    def test_later_admission_cannot_take_over_command_id_with_older_timestamp(self):
        with repository() as root:
            shared = command_id(25)
            first = issue(25, create_command(25, command_uuid=shared))
            reconcile(root, [first])
            older = issue(
                1,
                create_command(25, command_uuid=shared),
                issue_id=888001,
            )
            reconcile(root, [older])
            self.assertEqual(load_receipt(root, older)["code"], "identical_replay")
            self.assertEqual(len(replay(root, load_manifest(root)).events), 1)

    def test_oversized_body_becomes_durable_rejection(self):
        with repository() as root:
            oversized = issue(26, "x" * 33000, fenced=False)
            reconcile(root, [oversized])
            receipt = load_receipt(root, oversized)
            self.assertEqual(receipt["code"], "body_too_large")
            self.assertTrue(
                (root / f"state/requests/issue-{oversized['id']}.json").is_file()
            )

    def test_unique_and_collection_record_limits_reject_without_events(self):
        with repository() as root:
            duplicate = create_command(
                30,
                data=resource_data(
                    30, title="GitHub Actions documentation"
                ),
            )
            duplicate["data"]["url"] = "https://example.com/unique-path"
            duplicate_issue = issue(30, duplicate)
            reconcile(root, [duplicate_issue])
            self.assertEqual(load_receipt(root, duplicate_issue)["code"], "unique")
            self.assertEqual(len(replay(root, load_manifest(root)).events), 0)

        with repository() as root:
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["limits"]["records_per_collection"] = 2
            manifest["limits"]["snapshot_items"] = 2
            manifest_path.write_bytes(canonical_bytes(manifest))
            full_issue = issue(31, create_command(31))
            reconcile(root, [full_issue])
            self.assertEqual(load_receipt(root, full_issue)["code"], "record_limit")

    def test_event_chain_tampering_is_detected(self):
        with repository() as root:
            first = issue(40, create_command(40))
            second = issue(41, create_command(41))
            reconcile(root, [first, second])
            events = sorted((root / "state/events").glob("*.json"))
            value = json.loads(events[1].read_text())
            value["previous_hash"] = "f" * 64
            events[1].write_bytes(canonical_bytes(value))
            with self.assertRaises(RappError) as raised:
                replay(root, load_manifest(root))
            self.assertIn(raised.exception.code, {"event_chain", "event_hash"})

    def test_build_is_byte_identical_and_versions_are_append_only(self):
        with repository() as root:
            first = issue(50, create_command(50), actor_id=505)
            reconcile(root, [first])
            manifest = load_manifest(root)
            build(root, manifest)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / "versions").rglob("*.json")
                if path.name != "index.json"
            }
            receipt = load_receipt(root, first)
            update = {
                "schema": "rapp-base-command/1.0",
                "command_id": command_id(51),
                "operation": "update",
                "collection": "resources",
                "record_id": receipt["record"]["id"],
                "if_revision": receipt["record"]["revision"],
                "data": {"rating": 5},
            }
            reconcile(root, [issue(51, update, actor_id=505)])
            build(root, manifest)
            for relative, data in before.items():
                self.assertEqual((root / relative).read_bytes(), data)
            snapshot = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for directory in ("api", "versions")
                for path in (root / directory).rglob("*.json")
            }
            snapshot["registry.json"] = (root / "registry.json").read_bytes()
            summary = build(root, manifest)
            self.assertEqual(summary["changed"], 0)
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for directory in ("api", "versions")
                for path in (root / directory).rglob("*.json")
            }
            after["registry.json"] = (root / "registry.json").read_bytes()
            self.assertEqual(snapshot, after)

    def test_missing_or_mutated_indexed_version_fails(self):
        with repository() as root:
            build(root, load_manifest(root))
            index = json.loads((root / "versions/index.json").read_text())
            victim = root / index["entries"][0]["path"]
            victim.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(RappError) as raised:
                build(root, load_manifest(root))
            self.assertEqual(raised.exception.code, "immutable_conflict")

    def test_generated_collection_byte_limit_is_enforced(self):
        with repository() as root:
            path = root / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["limits"]["generated_collection_bytes"] = 100
            path.write_bytes(canonical_bytes(manifest))
            with self.assertRaises(RappError) as raised:
                build(root, load_manifest(root))
            self.assertEqual(raised.exception.code, "generated_bytes")

    def test_end_to_end_fixture_to_raw_record_and_receipt(self):
        fixture = json.loads(
            (PROJECT_ROOT / "tests/fixtures/issues.json").read_text(encoding="utf-8")
        )
        with repository() as root:
            from rapp_base.reconcile import reconcile_document

            summary = reconcile_document(root, load_manifest(root), fixture)
            self.assertEqual(summary["applied"], 1)
            build(root, load_manifest(root))
            receipt = next(iter(load_receipts(root, load_manifest(root)).values()))
            record_path = (
                root
                / "api/v1/collections/resources/records"
                / f"{receipt['record']['id']}.json"
            )
            record = json.loads(record_path.read_text())
            self.assertEqual(record["owner_id"], 7001)
            self.assertEqual(record["data"]["title"], "Fixture resource")
            public_receipt = root / "api/v1/receipts/issue-701.json"
            self.assertTrue(public_receipt.is_file())

    def test_path_traversal_is_terminal_rejection(self):
        with repository() as root:
            command = create_command(60)
            command["collection"] = "../../state"
            bad = issue(60, command)
            reconcile(root, [bad])
            self.assertEqual(load_receipt(root, bad)["code"], "invalid_path")
            self.assertEqual(list((root / "state/events").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
