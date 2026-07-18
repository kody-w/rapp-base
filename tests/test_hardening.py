from __future__ import annotations

import json
import unittest

from rapp_base.build import build
from rapp_base.errors import RappError
from rapp_base.jsonutil import canonical_bytes, object_hash, sha256_bytes
from rapp_base.manifest import genesis_sha256, load_manifest
from rapp_base.reconcile import load_requests
from rapp_base.state import head_for_events, replay

from helpers import (
    command_id,
    create_command,
    issue,
    load_receipt,
    reconcile,
    repository,
    resource_data,
)


class HardeningTests(unittest.TestCase):
    def assertCode(self, code, callable_value):
        with self.assertRaises(RappError) as raised:
            callable_value()
        self.assertEqual(raised.exception.code, code)

    def test_unlabeled_prefix_routes_and_unrelated_or_closed_issues_do_not(self):
        with repository() as root:
            routed = issue(101, create_command(101), labels=[])
            unrelated = issue(
                102,
                create_command(102),
                title="Question about RAPP Base",
            )
            closed = issue(103, create_command(103))
            closed["state"] = "closed"
            summary = reconcile(root, [unrelated, closed, routed])
            self.assertEqual(summary["admitted"], 1)
            self.assertEqual(load_receipt(root, routed)["status"], "applied")
            self.assertFalse(
                (root / f"state/requests/issue-{unrelated['id']}.json").exists()
            )
            self.assertFalse(
                (root / f"state/requests/issue-{closed['id']}.json").exists()
            )
            request = json.loads(
                (root / f"state/requests/issue-{routed['id']}.json").read_text()
            )
            self.assertEqual(request["issue"]["title"], routed["title"])

    def test_versions_are_addressed_by_stored_bytes_and_other_majors_survive(self):
        with repository() as root:
            sentinel = root / "api/v2/keep.json"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('{"schema":"future/2.0"}\n', encoding="utf-8")
            reconcile(root, [issue(100, create_command(100))])
            build(root, load_manifest(root))
            index = json.loads((root / "versions/index.json").read_text())
            self.assertGreater(len(index["entries"]), 0)
            self.assertIn("request", {entry["kind"] for entry in index["entries"]})
            for entry in index["entries"]:
                data = (root / entry["path"]).read_bytes()
                self.assertEqual(entry["content_sha256"], sha256_bytes(data))
                self.assertEqual(
                    entry["path"].rsplit("/", 1)[-1][:-5],
                    entry["content_sha256"][:12],
                )
                self.assertRegex(entry["semantic_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(sentinel.read_text(), '{"schema":"future/2.0"}\n')

    def test_zero_event_genesis_reanchors_but_post_event_schema_change_fails(self):
        with repository() as root:
            original = load_manifest(root)
            original_hash = genesis_sha256(original)
            path = root / "manifest.json"
            value = json.loads(path.read_text())
            value["collections"][1]["fields"]["title"]["maxLength"] = 121
            path.write_bytes(canonical_bytes(value))
            customized = load_manifest(root)
            self.assertNotEqual(genesis_sha256(customized), original_hash)
            build(root, customized)
            head = json.loads((root / "state/head.json").read_text())
            self.assertEqual(head["genesis_sha256"], genesis_sha256(customized))

        with repository() as root:
            routed = issue(104, create_command(104))
            reconcile(root, [routed])
            path = root / "manifest.json"
            value = json.loads(path.read_text())
            value["collections"][1]["fields"]["title"]["maxLength"] = 121
            path.write_bytes(canonical_bytes(value))
            self.assertCode(
                "migration_required",
                lambda: replay(root, load_manifest(root)),
            )

        with repository() as root:
            reconcile(root, [issue(106, create_command(106))])
            path = root / "manifest.json"
            value = json.loads(path.read_text())
            value["collections"][1]["seed"][0]["data"]["rating"] = 4
            path.write_bytes(canonical_bytes(value))
            self.assertCode(
                "migration_required",
                lambda: replay(root, load_manifest(root)),
            )

    def test_policy_description_and_safe_limit_changes_do_not_reinterpret_events(self):
        with repository() as root:
            routed = issue(105, create_command(105))
            reconcile(root, [routed])
            path = root / "manifest.json"
            value = json.loads(path.read_text())
            value["description"] = "Updated operator description."
            value["collections"][1]["policies"]["create"] = "maintainer"
            value["limits"]["issues_per_reconcile"] = 99
            value["limits"]["json_nodes"] = 1
            path.write_bytes(canonical_bytes(value))
            manifest = load_manifest(root)
            self.assertEqual(len(replay(root, manifest).events), 1)
            build(root, manifest)

    def test_all_malformed_shapes_are_durable_and_do_not_poison_later_builds(self):
        with repository() as root:
            long_string = create_command(
                110,
                data={**resource_data(110), "summary": "x" * 5120},
            )
            many_keys = create_command(
                111,
                data={f"unknown_{index}": index for index in range(60)},
            )
            large_array = create_command(
                112,
                data={**resource_data(112), "topics": [str(i) for i in range(101)]},
            )
            nested = "leaf"
            for index in range(10):
                nested = {f"level_{index}": nested}
            too_deep = create_command(113, data={"nested": nested})
            malformed = issue(114, "{not json", fenced=False)
            bad_wrapper = issue(115, "not a command", fenced=False)
            good = issue(116, create_command(116))
            values = [
                issue(110, long_string),
                issue(111, many_keys),
                issue(112, large_array),
                issue(113, too_deep),
                malformed,
                bad_wrapper,
                good,
            ]
            reconcile(root, values)
            self.assertEqual(load_receipt(root, values[0])["code"], "string_too_large")
            self.assertEqual(load_receipt(root, values[1])["code"], "too_many_keys")
            self.assertEqual(load_receipt(root, values[2])["code"], "array_too_large")
            self.assertEqual(load_receipt(root, values[3])["code"], "too_deep")
            self.assertEqual(load_receipt(root, malformed)["code"], "invalid_json")
            self.assertEqual(
                load_receipt(root, bad_wrapper)["code"],
                "invalid_issue_form",
            )
            self.assertEqual(load_receipt(root, good)["status"], "applied")
            self.assertEqual(len(load_requests(root, load_manifest(root))), 7)
            build(root, load_manifest(root))

    def test_deleted_event_cannot_be_replaced_by_forged_rejection(self):
        with repository() as root:
            routed = issue(120, create_command(120))
            reconcile(root, [routed])
            manifest = load_manifest(root)
            event_path = next((root / "state/events").glob("*.json"))
            event_path.unlink()
            (root / "state/head.json").write_bytes(
                canonical_bytes(head_for_events(manifest, []))
            )
            receipt_path = root / f"state/receipts/issue-{routed['id']}.json"
            forged = json.loads(receipt_path.read_text())
            forged.update(
                {
                    "code": "forbidden",
                    "event": None,
                    "message": "the GitHub actor is not authorized for this operation",
                    "record": None,
                    "status": "rejected",
                    "receipt_id": "0" * 64,
                }
            )
            forged["receipt_id"] = object_hash(forged, "receipt_id")
            receipt_path.write_bytes(canonical_bytes(forged))
            self.assertCode(
                "history_mismatch",
                lambda: build(root, manifest, write=False),
            )

    def test_lagging_head_is_reported_read_only_and_repaired_in_write_mode(self):
        with repository() as root:
            routed = issue(121, create_command(121))
            reconcile(root, [routed])
            manifest = load_manifest(root)
            stale = canonical_bytes(head_for_events(manifest, []))
            head_path = root / "state/head.json"
            head_path.write_bytes(stale)
            self.assertCode(
                "stale_head",
                lambda: build(root, manifest, write=False),
            )
            self.assertEqual(head_path.read_bytes(), stale)
            build(root, manifest, write=True)
            self.assertEqual(json.loads(head_path.read_text())["sequence"], 1)

    def test_exact_orphan_version_is_adopted_but_check_does_not_mutate(self):
        with repository() as root:
            manifest = load_manifest(root)
            build(root, manifest)
            index_path = root / "versions/index.json"
            index = json.loads(index_path.read_text())
            victim = index["entries"].pop()
            index["totalItems"] -= 1
            index_path.write_bytes(canonical_bytes(index))
            stale = index_path.read_bytes()
            self.assertCode(
                "build_out_of_date",
                lambda: build(root, manifest, write=False),
            )
            self.assertEqual(index_path.read_bytes(), stale)
            build(root, manifest, write=True)
            repaired = json.loads(index_path.read_text())
            self.assertIn(victim, repaired["entries"])
            unexpected = root / "versions/requests/000000000000.json"
            unexpected.parent.mkdir(parents=True, exist_ok=True)
            unexpected.write_text('{"schema":"unexpected/1.0"}\n', encoding="utf-8")
            self.assertCode(
                "unindexed_version",
                lambda: build(root, manifest, write=True),
            )

    def test_fractional_timestamps_use_instants_and_issue_id_tiebreaks(self):
        with repository() as root:
            early = issue(130, create_command(130))
            late = issue(131, create_command(131))
            early["created_at"] = early["updated_at"] = "2026-07-18T20:00:00.10Z"
            late["created_at"] = late["updated_at"] = "2026-07-18T20:00:00.9Z"
            reconcile(root, [late, early])
            requests = load_requests(root, load_manifest(root))
            self.assertEqual(requests[early["id"]]["admission_sequence"], 1)
            self.assertEqual(requests[late["id"]]["admission_sequence"], 2)
            tie_low = issue(133, create_command(133))
            tie_high = issue(134, create_command(134))
            for value in (tie_low, tie_high):
                value["created_at"] = value["updated_at"] = (
                    "2026-07-18T20:00:00.9Z"
                )
            reconcile(root, [tie_high, tie_low])
            requests = load_requests(root, load_manifest(root))
            self.assertLess(
                requests[tie_low["id"]]["admission_sequence"],
                requests[tie_high["id"]]["admission_sequence"],
            )

            created = load_receipt(root, late)
            delete = {
                "schema": "rapp-base-command/1.0",
                "command_id": command_id(132),
                "operation": "delete",
                "collection": "resources",
                "record_id": created["record"]["id"],
                "if_revision": created["record"]["revision"],
            }
            deletion = issue(132, delete, actor_id=late["user"]["id"])
            deletion["created_at"] = deletion["updated_at"] = (
                "2026-07-18T20:00:00.10Z"
            )
            reconcile(root, [deletion])
            state = replay(root, load_manifest(root))
            tombstone = state.records["resources"][created["record"]["id"]]
            self.assertEqual(tombstone["deleted_at"], "2026-07-18T20:00:00.9Z")
            build(root, load_manifest(root))
            status = json.loads((root / "api/v1/status.json").read_text())
            self.assertEqual(status["generated_at"], "2026-07-18T20:00:00.9Z")


if __name__ == "__main__":
    unittest.main()
