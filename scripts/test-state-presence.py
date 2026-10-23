import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("state_presence", Path(__file__).with_name("state-presence.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PresenceTests(unittest.TestCase):
    def test_missing_empty_and_managed(self):
        class Missing(Exception): pass
        def missing(**kwargs): raise Missing()
        client = SimpleNamespace(get_object=missing, exceptions=SimpleNamespace(NoSuchKey=Missing))
        self.assertEqual(module.presence(client, "techx-state"), dict(exists=False, managedCount=0))
        for resources, count in (([], 0), ([dict(mode="managed"), dict(mode="data")], 1)):
            client.get_object = lambda **kwargs: dict(Body=io.BytesIO(json.dumps(dict(version=4, resources=resources)).encode()))
            self.assertEqual(module.presence(client, "techx-state"), dict(exists=True, managedCount=count))

    def test_access_failure_or_invalid_state_is_not_absence(self):
        class Missing(Exception): pass
        def denied(**kwargs): raise PermissionError("AccessDenied")
        client = SimpleNamespace(get_object=denied, exceptions=SimpleNamespace(NoSuchKey=Missing))
        with self.assertRaises(PermissionError): module.presence(client, "techx-state")
        client.get_object = lambda **kwargs: dict(Body=io.BytesIO(b'{}'))
        with self.assertRaises(ValueError): module.presence(client, "techx-state")


if __name__ == "__main__": unittest.main()
