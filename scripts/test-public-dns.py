import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import dns.flags
import dns.message
import dns.rcode
import dns.rrset

spec = importlib.util.spec_from_file_location("dns_guard", Path(__file__).with_name("public-dns.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DnsTests(unittest.TestCase):
    def test_positive_dns_requires_one_exact_shop_target(self):
        answer = dict(type=5, name=module.HOST + ".", data="D123.CLOUDFRONT.NET.")
        module.assert_target(dict(Status=0, Answer=[answer]), "d123.cloudfront.net")
        for response in (dict(Status=3), dict(Status=2), dict(Status=0, Answer=[]),
                dict(Status=0, Answer=[dict(answer, data="dother.cloudfront.net")]),
                dict(Status=0, Answer=[dict(answer, name="other.example")]), dict(Status=0, Answer=[answer, answer])):
            with self.subTest(response=response), self.assertRaises(ValueError):
                module.assert_target(response, "d123.cloudfront.net")

    def test_positive_authoritative_proof_rejects_negative_wrong_target_or_missing_aa(self):
        query = dns.message.make_query(module.HOST, "CNAME")
        response = dns.message.make_response(query)
        response.flags |= dns.flags.AA
        response.answer.append(dns.rrset.from_text(module.HOST, 60, "IN", "CNAME", "d123.cloudfront.net."))
        module.assert_authoritative(response, query, "d123.cloudfront.net")
        with self.assertRaises(ValueError): module.assert_authoritative(response, query, "dother.cloudfront.net")
        response.flags &= ~dns.flags.AA
        with self.assertRaises(ValueError): module.assert_authoritative(response, query, "d123.cloudfront.net")
        query, response = self.negative_response()
        with self.assertRaises(ValueError): module.assert_authoritative(response, query, "d123.cloudfront.net")

    def test_activation_verifies_both_recursive_resolvers_and_every_authority(self):
        recursive, authorities, guards = [], [], []
        def open_url(request, **kw):
            recursive.append(request.full_url)
            return io.BytesIO(json.dumps(dict(Status=0, Answer=[dict(type=5, name=module.HOST, data="d123.cloudfront.net")])).encode())
        def doh(endpoint, name, kind):
            if kind == "NS": return [dict(type=2, name=module.ZONE, data=name + ".ns.cloudflare.com") for name in ("a", "b")]
            return [dict(type=1, data="8.8.8.8" if name.startswith("a.") else "1.1.1.1")]
        def query_authority(query, address, **kw):
            authorities.append(address)
            response = dns.message.make_response(query)
            response.flags |= dns.flags.AA
            response.answer.append(dns.rrset.from_text(module.HOST, 60, "IN", "CNAME", "d123.cloudfront.net."))
            return response, False
        with patch.object(module, "urlopen", side_effect=open_url), patch.object(module, "doh", side_effect=doh), \
                patch.object(module.dns.query, "udp_with_fallback", side_effect=query_authority):
            module.verify_target("d123.cloudfront.net", guard=lambda: guards.append(True))
        self.assertEqual(len(recursive), 2)
        self.assertEqual(authorities, ["8.8.8.8", "1.1.1.1"])
        self.assertEqual(len(guards), 8)

    def test_fenced_activation_cannot_query_dns(self):
        with patch.object(module, "urlopen") as request:
            with self.assertRaisesRegex(ValueError, "fenced"):
                module.verify_target("d123.cloudfront.net", guard=lambda: (_ for _ in ()).throw(ValueError("fenced")))
            request.assert_not_called()

    def test_absent_record(self):
        module.assert_no_cloudfront({"Status": 3})
        module.assert_no_cloudfront({"Status": 0})

    def test_live_record_or_unknown_never_passes(self):
        for response in ({}, {"Status": 2}, {"Status": 5}, {"Status": 3, "Answer": [1]},
                         {"Status": 0, "Answer": [{"type": 5, "data": "d123.CLOUDFRONT.NET."}]}):
            with self.subTest(response=response):
                with self.assertRaises(ValueError): module.assert_no_cloudfront(response)

    def test_network_failure_is_not_absent_dns(self):
        with patch.object(module, "urlopen", side_effect=TimeoutError("resolver unavailable")):
            with self.assertRaises(TimeoutError): module.verify()

    def negative_response(self):
        query = dns.message.make_query(module.HOST, "CNAME")
        response = dns.message.make_response(query)
        response.flags |= dns.flags.AA
        response.set_rcode(dns.rcode.NXDOMAIN)
        response.authority.append(dns.rrset.from_text(module.ZONE + ".", 300, "IN", "SOA",
                                                     "a.ns.cloudflare.com. dns.cloudflare.com. 1 2 3 4 5"))
        return query, response

    def test_authority_proves_absence(self):
        query, response = self.negative_response()
        module.assert_authoritative(response, query)
        response.set_rcode(dns.rcode.NOERROR)
        module.assert_authoritative(response, query)

    def test_rejects_non_authority_missing_soa_live_cname_and_wrong_question(self):
        for mutation in ("aa", "soa", "live", "question", "error"):
            query, response = self.negative_response()
            if mutation == "aa": response.flags &= ~dns.flags.AA
            if mutation == "soa": response.authority.clear()
            if mutation == "live":
                response.set_rcode(dns.rcode.NOERROR)
                response.answer.append(dns.rrset.from_text(module.HOST, 300, "IN", "CNAME", "d123.cloudfront.net."))
            if mutation == "question": response.question = dns.message.make_query("other.example", "CNAME").question
            if mutation == "error": response.set_rcode(dns.rcode.SERVFAIL)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError): module.assert_authoritative(response, query)


if __name__ == "__main__":
    unittest.main()
