import unittest
from cloudflare_dns import CloudflareDNS, HOST


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.records = [], []
        def request(method, path, payload=None):
            self.calls.append((method, path, payload))
            if method == "GET": return dict(result=self.records)
            if method == "POST":
                self.records = [dict(payload, id="b" * 32)]
                return dict(result=self.records[0])
            if method == "DELETE": self.records = []; return dict(result={})
            raise AssertionError("Unexpected operation")
        self.client = CloudflareDNS("a" * 32, "fake-token-never-logged", request)

    def test_create_retry_delete_retry(self):
        owner = self.client.ensure(HOST, "d123.cloudfront.net", "staging-1234")
        self.assertEqual(self.client.ensure(HOST, "d123.cloudfront.net", "staging-1234"), owner)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(self.client.delete(owner), "deleted")
        self.assertEqual(self.client.delete(owner), "already-absent")

    def test_never_touches_apex_unowned_or_changed_target(self):
        with self.assertRaises(ValueError): self.client.ensure("dinhminhkhoa.id.vn", "d123.cloudfront.net", "staging-1234")
        self.assertFalse(self.calls)
        owner = self.client.ensure(HOST, "d123.cloudfront.net", "staging-1234")
        self.records[0]["content"] = "d999.cloudfront.net"
        with self.assertRaises(ValueError): self.client.delete(owner)
        with self.assertRaises(ValueError): self.client.ensure(HOST, "d123.cloudfront.net", "staging-1234")
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))

    def test_acm_validation_scope(self):
        owner = self.client.ensure("_abc123." + HOST, "_abc.def.acm-validations.aws", "staging-1234")
        self.assertEqual(owner["name"], "_abc123." + HOST)
        with self.assertRaises(ValueError): self.client.ensure("_abc123.other.example", "_abc.def.acm-validations.aws", "staging-1234")


if __name__ == "__main__":
    unittest.main()
