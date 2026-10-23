"""Scoped DNS mutations with session ownership; credentials are never returned."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = "shop.dinhminhkhoa.id.vn"


def validate_name_target(name, target):
    name, target = name.rstrip(".").lower(), target.rstrip(".").lower()
    if name == HOST:
        if not re.fullmatch(r"d[a-z0-9]+\.cloudfront\.net", target):
            raise ValueError("Storefront must target a CloudFront distribution")
    elif re.fullmatch(r"_[a-f0-9]+\." + re.escape(HOST), name):
        if not re.fullmatch(r"_[a-z0-9]+\.[a-z0-9]+\.acm-validations\.aws", target):
            raise ValueError("Validation must target ACM")
    else:
        raise ValueError("Only shop or its ACM validation CNAME may be managed")
    return name, target


class CloudflareDNS:
    def __init__(self, zone_id, token, request=None):
        if not re.fullmatch(r"[a-f0-9]{32}", zone_id) or not token:
            raise ValueError("Cloudflare zone ID and scoped token are required")
        self.zone_id, self.token = zone_id, token
        self.request = request or self._request

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records{path}",
                          data=body, method=method, headers={"Authorization": f"Bearer {self.token}",
                                                           "Content-Type": "application/json"})
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
        if result.get("success") is not True:
            raise ValueError("Cloudflare request failed; inspect API status without logging credentials")
        return result

    def records(self, name):
        result = self.request("GET", "?" + urlencode(dict(type="CNAME", name=name, per_page=100)))
        if result.get("result_info", {}).get("total_pages", 1) != 1:
            raise ValueError("Ambiguous DNS record result")
        return result["result"]

    def ensure(self, name, target, session_id):
        name, target = validate_name_target(name, target)
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", session_id):
            raise ValueError("Invalid session ID")
        comment = f"techx:{session_id}"
        records = self.records(name)
        if len(records) > 1:
            raise ValueError("Duplicate DNS records require review")
        payload = dict(type="CNAME", name=name, content=target, proxied=False, ttl=1, comment=comment)
        if records:
            record = records[0]
            if record.get("comment") != comment or record["content"].rstrip(".").lower() != target:
                raise ValueError("Existing DNS record is not owned by this session/target")
            if record.get("proxied") is not False:
                raise ValueError("Owned CNAME must remain DNS-only")
            return dict(id=record["id"], name=name, content=target, sessionId=session_id)
        result = self.request("POST", "", payload)["result"]
        return dict(id=result["id"], name=name, content=target, sessionId=session_id)

    def delete(self, ownership):
        name, target = validate_name_target(ownership["name"], ownership["content"])
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", ownership["sessionId"]):
            raise ValueError("Invalid ownership session ID")
        records = self.records(name)
        if not records:
            return "already-absent"
        if len(records) != 1:
            raise ValueError("Ambiguous record deletion refused")
        record = records[0]
        if record["id"] != ownership["id"] or record["content"].rstrip(".").lower() != target or record.get("comment") != f"techx:{ownership['sessionId']}":
            raise ValueError("DNS ownership changed; refusing deletion")
        if not re.fullmatch(r"[a-f0-9]{32}", record["id"]):
            raise ValueError("Invalid DNS record ID")
        self.request("DELETE", "/" + record["id"])
        return "deleted"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "delete"))
    parser.add_argument("--ownership", type=Path, required=True)
    parser.add_argument("--name")
    parser.add_argument("--target")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    client = CloudflareDNS(os.environ.get("CLOUDFLARE_ZONE_ID", ""), os.environ.get("CLOUDFLARE_API_TOKEN", ""))
    if args.action == "ensure":
        result = client.ensure(args.name or "", args.target or "", args.session_id or "")
        args.ownership.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print("Owned DNS-only CNAME is ready; ownership record saved.")
    else:
        print(client.delete(json.loads(args.ownership.read_text(encoding="utf-8"))))
