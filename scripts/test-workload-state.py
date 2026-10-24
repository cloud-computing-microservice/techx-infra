from copy import deepcopy
import io
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from workload_state import KEY, ensure_empty_state


class StateInitTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.writes = {}, []
        class Missing(Exception): pass
        class Conflict(Exception): response = dict(Error=dict(Code="PreconditionFailed"))
        self.conflict = Conflict
        def get_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], "058114477594")
            if kwargs["Key"] not in self.objects: raise Missing()
            return dict(Body=io.BytesIO(self.objects[kwargs["Key"]]), ETag=hashlib.sha256(self.objects[kwargs["Key"]]).hexdigest())
        def put_object(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if kwargs["Key"] in self.objects: raise Conflict()
            self.objects[kwargs["Key"]] = kwargs["Body"]
            self.writes.append(kwargs["Key"])
        def delete_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], "058114477594")
            if hashlib.sha256(self.objects[kwargs["Key"]]).hexdigest() != kwargs["IfMatch"]: raise Conflict()
            del self.objects[kwargs["Key"]]
        self.client = SimpleNamespace(get_object=get_object, put_object=put_object, delete_object=delete_object, exceptions=SimpleNamespace(NoSuchKey=Missing))
        self.inventory = dict(accountId="058114477594", region="us-east-1", environment="staging")
        for key in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[key] = []

    def test_absent_state_is_created_once_and_existing_lineage_never_replaced(self):
        first = ensure_empty_state(self.client, "test-control", self.inventory)
        second = ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertEqual(first, second)
        self.assertEqual(first["serial"], 0)
        self.assertEqual(self.writes, [KEY + ".tflock", KEY, KEY + ".tflock"])
        self.assertNotIn(KEY + ".tflock", self.objects)

    def test_nonempty_inventory_or_backend_lock_prevents_writes(self):
        self.inventory["clusters"] = ["techx-staging"]
        with self.assertRaises(ValueError): ensure_empty_state(self.client, "test-control", self.inventory)
        self.inventory["clusters"] = []
        self.objects[KEY + ".tflock"] = b"existing lock"
        with self.assertRaises(ValueError): ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertFalse(self.writes)

    def test_existing_managed_or_invalid_state_is_not_treated_as_empty(self):
        ensure_empty_state(self.client, "test-control", self.inventory)
        original = json.loads(self.objects[KEY])
        for resource in (dict(mode="managed"), dict(mode="unknown"), "invalid"):
            state = dict(original, resources=[resource])
            self.objects[KEY] = json.dumps(state).encode()
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                ensure_empty_state(self.client, "test-control", self.inventory)
        self.objects[KEY] = b"{}"
        with self.assertRaises(ValueError): ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertEqual(self.writes.count(KEY), 1)
        self.assertNotIn(KEY + ".tflock", self.objects)

    def test_api_error_is_not_state_absence(self):
        def denied(**kwargs): raise PermissionError("AccessDenied")
        self.client.get_object = denied
        with self.assertRaises(PermissionError): ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertNotIn(KEY, self.writes)

    def test_native_lock_prevents_concurrent_initializer_from_entering(self):
        original = self.client.put_object
        nested = []
        def concurrent(**kwargs):
            result = original(**kwargs)
            if kwargs["Key"] == KEY + ".tflock":
                with self.assertRaises(ValueError): ensure_empty_state(self.client, "test-control", self.inventory)
                nested.append("blocked")
            return result
        self.client.put_object = concurrent
        ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertEqual(nested, ["blocked"])
        self.assertEqual(self.writes.count(KEY), 1)

    def test_replaced_lock_is_preserved_and_admission_fails(self):
        original = self.client.get_object
        def replaced(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"] == KEY and self.writes.count(KEY) == 1:
                self.objects[KEY + ".tflock"] = b"somebody else's lock"
            return response
        self.client.get_object = replaced
        with self.assertRaises(self.conflict): ensure_empty_state(self.client, "test-control", self.inventory)
        self.assertEqual(self.objects[KEY + ".tflock"], b"somebody else's lock")


if __name__ == "__main__": unittest.main()
