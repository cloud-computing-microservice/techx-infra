import importlib.util
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
