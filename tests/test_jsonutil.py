from __future__ import annotations

import json
import unittest

from rapp_base.commands import parse_command_text
from rapp_base.errors import RappError
from rapp_base.jsonutil import extract_command_text, strict_loads
from rapp_base.manifest import load_manifest

from helpers import PROJECT_ROOT, command_id, create_command


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

    def test_multiple_json_candidates_and_trailing_text_fail(self):
        self.assertCode(
            "invalid_json",
            lambda: strict_loads("{} {}", self.limits, require_object=True),
        )
        self.assertCode(
            "invalid_json",
            lambda: strict_loads('{"ok":true} trailing', self.limits, require_object=True),
        )

    def test_exact_issue_form_fence_is_accepted(self):
        text = json.dumps(create_command(1), separators=(",", ":"))
        body = f"### Command\n\n```json\n{text}\n```"
        self.assertEqual(extract_command_text(body, self.limits), text)
        parsed = parse_command_text(extract_command_text(body, self.limits), self.limits)
        self.assertEqual(parsed["command_id"], command_id(1))

    def test_extra_markdown_and_multiple_fences_fail(self):
        text = json.dumps(create_command(1), separators=(",", ":"))
        self.assertCode(
            "invalid_issue_form",
            lambda: extract_command_text(f"intro\n### Command\n\n{text}", self.limits),
        )
        self.assertCode(
            "invalid_issue_form",
            lambda: extract_command_text(
                f"### Command\n\n```json\n{text}\n```\n```json\n{{}}\n```",
                self.limits,
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


if __name__ == "__main__":
    unittest.main()

