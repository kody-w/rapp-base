from __future__ import annotations

import copy
import unittest

from rapp_base.errors import RappError
from rapp_base.manifest import (
    assert_unique,
    authorize,
    collection_map,
    load_manifest,
    validate_data,
)

from helpers import PROJECT_ROOT, resource_data


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest(PROJECT_ROOT)
        cls.resources = collection_map(cls.manifest)["resources"]

    def assertCode(self, code, callable_value):
        with self.assertRaises(RappError) as raised:
            callable_value()
        self.assertEqual(raised.exception.code, code)

    def test_types_constraints_and_unknown_fields(self):
        value = resource_data(20)
        self.assertEqual(validate_data(self.resources, value), value)
        wrong = copy.deepcopy(value)
        wrong["free"] = 1
        self.assertCode("field_type", lambda: validate_data(self.resources, wrong))
        unknown = copy.deepcopy(value)
        unknown["surprise"] = True
        self.assertCode("unknown_field", lambda: validate_data(self.resources, unknown))
        missing = copy.deepcopy(value)
        del missing["title"]
        self.assertCode("required_field", lambda: validate_data(self.resources, missing))

    def test_enum_numeric_bounds_array_and_url_format(self):
        value = resource_data(21)
        value["kind"] = "video"
        self.assertCode("enum", lambda: validate_data(self.resources, value))
        value = resource_data(21, rating=6)
        self.assertCode("maximum", lambda: validate_data(self.resources, value))
        value = resource_data(21)
        value["topics"] = []
        self.assertCode("min_length", lambda: validate_data(self.resources, value))
        value = resource_data(21)
        value["url"] = "http://example.com/not-https"
        self.assertCode("url_format", lambda: validate_data(self.resources, value))

    def test_unique_fields_are_checked_against_live_records(self):
        data = resource_data(22)
        records = {"one": {"deleted": False, "data": data}}
        self.assertCode(
            "unique",
            lambda: assert_unique(self.resources, records, copy.deepcopy(data)),
        )
        records["one"]["deleted"] = True
        assert_unique(self.resources, records, copy.deepcopy(data))

    def test_policy_matrix_and_privileged_recovery(self):
        self.assertEqual(
            authorize("public", actor_id=1, association="NONE"), "public"
        )
        self.assertEqual(
            authorize("owner", actor_id=7, association="NONE", owner_id=7), "owner"
        )
        self.assertEqual(
            authorize("owner", actor_id=8, association="OWNER", owner_id=7),
            "repository_owner",
        )
        self.assertEqual(
            authorize("collaborator", actor_id=8, association="COLLABORATOR"),
            "collaborator",
        )
        self.assertCode(
            "forbidden",
            lambda: authorize(
                "owner",
                actor_id=8,
                association="COLLABORATOR",
                owner_id=7,
            ),
        )
        self.assertCode(
            "forbidden",
            lambda: authorize("collaborator", actor_id=8, association="MEMBER"),
        )
        self.assertCode(
            "forbidden",
            lambda: authorize("maintainer", actor_id=8, association="COLLABORATOR"),
        )
        self.assertCode(
            "forbidden",
            lambda: authorize("maintainer", actor_id=8, association="MEMBER"),
        )
        self.assertCode(
            "policy_disabled",
            lambda: authorize("disabled", actor_id=1, association="OWNER"),
        )


if __name__ == "__main__":
    unittest.main()
