from __future__ import annotations

import json
import math
import unittest
from unittest import mock

from rapp_base.commands import parse_command_text
from rapp_base.constants import (
    PUBLICATION_ATTESTATION,
    PUBLICATION_ATTESTATION_HEADING,
)
from rapp_base.errors import RappError
from rapp_base.jsonutil import (
    canonical_bytes,
    extract_command_text,
    render_issue_form_body,
    sha256_bytes,
    strict_loads,
    write_bytes_immutable,
)
from rapp_base.manifest import load_manifest

from helpers import PROJECT_ROOT, command_id, create_command, repository


class StrictJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.limits = load_manifest(PROJECT_ROOT)["limits"]

    def assertCode(self, code, callable_value):
        with self.assertRaises(RappError) as raised:
            callable_value()
        self.assertEqual(raised.exception.code, code)

    def test_duplicate_keys_fail(self):
        text = (
            '{"schema":"rapp-base-command/1.0",'
            f'"command_id":"{command_id(1)}","operation":"create",'
            '"collection":"resources","collection":"rapps","data":{}}'
        )
        self.assertCode("duplicate_key", lambda: parse_command_text(text, self.limits))

    def test_non_finite_numbers_fail(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                self.assertCode(
                    "invalid_number",
                    lambda token=token: strict_loads(
                        f'{{"value":{token}}}', self.limits, require_object=True
                    ),
                )

    def test_scalar_contract_normalizes_zero_rejects_unsafe_and_preserves_unicode(self):
        value = strict_loads(
            '{"float":-0.0,"exponent":-0e10,"integer":-0}',
            self.limits,
            require_object=True,
        )
        self.assertGreater(math.copysign(1, value["float"]), 0)
        self.assertGreater(math.copysign(1, value["exponent"]), 0)
        self.assertEqual(value["integer"], 0)
        for token in (
            "9007199254740992",
            "9007199254740991.1",
            "9007199254740992.0",
            "1e20",
        ):
            with self.subTest(token=token):
                self.assertCode(
                    "number_out_of_range",
                    lambda token=token: strict_loads(
                        f'{{"value":{token}}}', self.limits, require_object=True
                    ),
                )
        decomposed = strict_loads(
            '{"value":"e\\u0301","e\\u0301":"value"}',
            self.limits,
            require_object=True,
        )
        self.assertEqual(decomposed["value"], "e\u0301")
        self.assertEqual(decomposed["e\u0301"], "value")

    def test_canonical_byte_and_hash_vectors_are_stable(self):
        vectors = (
            (
                {"z": -0.0, "a": 1.25},
                '{"a":1.25,"z":0.0}\n',
                "54bb20358c12c227711ff7d4d644720a29b95549b255abe313b213dfe69ef2e3",
            ),
            (
                {"é": "café", "astral": "😀"},
                '{"astral":"😀","é":"café"}\n',
                "c8f277c8ca83426bda18efefae2a0df35f8a03393017516b067298a32a45ac2a",
            ),
            (
                {"z": 3, "aa": 2, "a": 1},
                '{"a":1,"aa":2,"z":3}\n',
                "3cea5b20a688384b4d9061ce0647dccee7f41d95d9c50c53e4c282b6335073ad",
            ),
            (
                {"value": "e\u0301"},
                '{"value":"é"}\n',
                "76ffc8183b39bef5ef5474268efe0a76de74f4c38bf77f72dacdbfebd8ee4284",
            ),
        )
        for value, text, digest in vectors:
            with self.subTest(text=text):
                encoded = canonical_bytes(value)
                self.assertEqual(encoded, text.encode("utf-8"))
                self.assertEqual(sha256_bytes(encoded), digest)

    def test_multiple_json_candidates_and_trailing_text_fail(self):
        self.assertCode(
            "invalid_json",
            lambda: strict_loads("{} {}", self.limits, require_object=True),
        )
        self.assertCode(
            "invalid_json",
            lambda: strict_loads('{"ok":true} trailing', self.limits, require_object=True),
        )

    def test_raw_legacy_v1_sdk_and_checked_issue_form_bodies_are_accepted(self):
        text = json.dumps(create_command(1), ensure_ascii=False, indent=2)
        legacy_body = f"### Command\n\n```json\n{text}\n```"
        self.assertEqual(extract_command_text(text, self.limits), text)
        self.assertEqual(extract_command_text(legacy_body, self.limits), text)
        for checked in ("x", "X"):
            with self.subTest(checked=checked):
                body = render_issue_form_body(text).replace(
                    "- [X] ", f"- [{checked}] "
                )
                self.assertEqual(extract_command_text(body, self.limits), text)
        parsed = parse_command_text(
            extract_command_text(legacy_body, self.limits),
            self.limits,
        )
        self.assertEqual(parsed["command_id"], command_id(1))

    def test_issue_form_declares_the_exact_required_attestation(self):
        form = (
            PROJECT_ROOT / ".github/ISSUE_TEMPLATE/rapp-base-command.yml"
        ).read_text(encoding="utf-8")
        expected = (
            "  - type: checkboxes\n"
            "    id: publication_attestation\n"
            "    attributes:\n"
            f"      label: {PUBLICATION_ATTESTATION_HEADING}\n"
            "      options:\n"
            f"        - label: {PUBLICATION_ATTESTATION}\n"
            "          required: true"
        )
        self.assertIn(expected, form)
        self.assertEqual(form.count(PUBLICATION_ATTESTATION), 1)

    def test_unchecked_modified_duplicate_and_extra_attestation_text_fail(self):
        text = json.dumps(create_command(1), separators=(",", ":"))
        body = render_issue_form_body(text)
        suffix_start = body.index(f"### {PUBLICATION_ATTESTATION_HEADING}")
        invalid = (
            body.replace("- [X] ", "- [ ] "),
            body.replace("all rights needed", "permission"),
            f"{body}\n\n{body[suffix_start:]}",
            f"{body}\nextra attestation text",
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate[-80:]):
                self.assertCode(
                    "invalid_issue_form",
                    lambda candidate=candidate: extract_command_text(
                        candidate, self.limits
                    ),
                )

    def test_extra_markdown_fences_and_sections_fail(self):
        text = json.dumps(create_command(1), separators=(",", ":"))
        body = render_issue_form_body(text)
        legacy_body = f"### Command\n\n```json\n{text}\n```"
        invalid = (
            f"intro\n{body}",
            body.replace(
                f"\n\n### {PUBLICATION_ATTESTATION_HEADING}",
                "\n\n### Unexpected section\n\nextra"
                f"\n\n### {PUBLICATION_ATTESTATION_HEADING}",
            ),
            f"{body}\n\n### Unexpected section\n\nextra",
            body.replace(text, f"{text}\n```\n```json\n{{}}"),
            f"{legacy_body}\ntrailing text",
            legacy_body.replace(text, f"{text}\n```\n```json\n{{}}"),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate[:80]):
                self.assertCode(
                    "invalid_issue_form",
                    lambda candidate=candidate: extract_command_text(
                        candidate, self.limits
                    ),
                )

    def test_control_characters_fail_even_when_escaped(self):
        self.assertCode(
            "control_character",
            lambda: strict_loads('{"value":"\\u0001"}', self.limits),
        )

    def test_depth_nodes_strings_and_arrays_are_bounded(self):
        small = {**self.limits, "json_depth": 2, "json_nodes": 3, "array_items": 2, "string_bytes": 3}
        self.assertCode("too_deep", lambda: strict_loads('{"a":{"b":1}}', small))
        self.assertCode("too_many_nodes", lambda: strict_loads('{"a":1,"b":2,"c":3}', small))
        self.assertCode("array_too_large", lambda: strict_loads("[1,2,3]", small))
        self.assertCode("string_too_large", lambda: strict_loads('"four"', small))

    def test_paths_and_unknown_command_keys_fail_closed(self):
        command = create_command(2)
        command["collection"] = "../resources"
        self.assertCode(
            "invalid_path",
            lambda: parse_command_text(json.dumps(command), self.limits),
        )
        command = create_command(2)
        command["actor_id"] = 7
        self.assertCode(
            "unknown_key",
            lambda: parse_command_text(json.dumps(command), self.limits),
        )

    def test_conditional_command_shape_is_strict(self):
        command = create_command(3)
        command["record_id"] = "client-picked"
        self.assertCode(
            "invalid_command_shape",
            lambda: parse_command_text(json.dumps(command), self.limits),
        )
        command = {
            "schema": "rapp-base-command/1.0",
            "command_id": command_id(3),
            "operation": "delete",
            "collection": "resources",
            "record_id": "safe-id",
            "if_revision": "a" * 64,
            "data": {},
        }
        self.assertCode(
            "invalid_command_shape",
            lambda: parse_command_text(json.dumps(command), self.limits),
        )

    def test_immutable_publication_is_complete_and_identical_is_a_noop(self):
        with repository() as root:
            target = root / "state/events/publication.json"
            data = b"complete immutable bytes\n"
            self.assertTrue(write_bytes_immutable(target, data))
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse(write_bytes_immutable(target, data))
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.stage")), [])

    def test_immutable_publication_detects_existing_conflict(self):
        with repository() as root:
            target = root / "state/events/conflict.json"
            target.write_bytes(b"prior\n")
            self.assertCode(
                "immutable_conflict",
                lambda: write_bytes_immutable(target, b"different\n"),
            )
            self.assertEqual(target.read_bytes(), b"prior\n")

    def test_failed_immutable_publication_leaves_no_target_or_staging(self):
        with repository() as root:
            target = root / "state/events/failure.json"
            with mock.patch(
                "rapp_base.jsonutil.os.link",
                side_effect=OSError("simulated publication failure"),
            ):
                with self.assertRaises(OSError):
                    write_bytes_immutable(target, b"never published\n")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.stage")), [])


if __name__ == "__main__":
    unittest.main()
