"""Require recursive and authoritative DNS convergence before deleting an origin."""
import argparse
import ipaddress
import json
import re
from urllib.request import Request, urlopen

import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype

HOST = "shop.dinhminhkhoa.id.vn"
ZONE = "dinhminhkhoa.id.vn"
ENDPOINTS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")


def assert_no_cloudfront(response):
    if response.get("Status") not in (0, 3):
        raise ValueError("Public DNS returned unknown/error status")
    answers = response.get("Answer", [])
    if response["Status"] == 3 and answers:
        raise ValueError("Inconsistent NXDOMAIN response")
    for answer in answers:
        if answer.get("type") == 5 and str(answer.get("data", "")).rstrip(".").lower().endswith(".cloudfront.net"):
            raise ValueError("Remove the Cloudflare shop CNAME first to avoid dangling DNS")


def verify():
    for endpoint in ENDPOINTS:
        request = Request(f"{endpoint}?name={HOST}&type=CNAME", headers={"Accept": "application/dns-json"})
        with urlopen(request, timeout=15) as response:
            assert_no_cloudfront(json.load(response))
    verify_authoritative()


def doh(endpoint, name, kind):
    request = Request(f"{endpoint}?name={name}&type={kind}", headers={"Accept": "application/dns-json"})
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get("Status") != 0:
        raise ValueError("Unable to resolve the zone authority")
    return result.get("Answer", [])


def assert_authoritative(response, query):
    if not query.is_response(response) or not response.flags & dns.flags.AA:
        raise ValueError("DNS response is not authoritative for the query")
    if response.rcode() not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
        raise ValueError("Authoritative DNS returned an error")
    answers = []
    for rrset in response.answer:
        if rrset.rdtype == dns.rdatatype.CNAME:
            answers.extend(dict(type=5, data=str(record.target)) for record in rrset)
    assert_no_cloudfront(dict(Status=response.rcode(), Answer=answers))
    if not response.answer:
        zone = dns.name.from_text(ZONE)
        if not any(rrset.rdtype == dns.rdatatype.SOA and rrset.name == zone for rrset in response.authority):
            raise ValueError("Negative DNS response lacks zone authority proof")


def verify_authoritative():
    discovered = []
    for endpoint in ENDPOINTS:
        names = {str(answer["data"]).rstrip(".").lower() for answer in doh(endpoint, ZONE, "NS")
                 if answer.get("type") == 2 and str(answer.get("name", "")).rstrip(".").lower() == ZONE}
        if len(names) < 2 or any(not re.fullmatch(r"[a-z0-9-]+\.ns\.cloudflare\.com", name) for name in names):
            raise ValueError("Expected the Cloudflare authority nameservers for this zone")
        discovered.append(names)
    if discovered[0] != discovered[1]:
        raise ValueError("Public resolvers disagree on zone delegation")
    for server in sorted(discovered[0]):
        addresses = {str(answer["data"]) for answer in doh(ENDPOINTS[0], server, "A") if answer.get("type") == 1}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("Invalid authoritative server address")
        query = dns.message.make_query(HOST, dns.rdatatype.CNAME)
        query.flags &= ~dns.flags.RD
        response, _ = dns.query.udp_with_fallback(query, sorted(addresses)[0], timeout=10)
        assert_authoritative(response, query)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    verify()
    print("Public recursive and authoritative DNS no longer point shop to CloudFront.")
