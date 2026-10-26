"""Bounded public storefront checks; no proxy, redirects or private destinations.

The protected caller proves a GitHub-hosted execution context and owns the
session lease. This module performs real HTTP/TLS checks and never marks READY.
"""
from copy import deepcopy
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from urllib.parse import quote

from cloudflare_dns import HOST

MAX_BODY = 2 * 1024 * 1024
CHECKS = {"dns", "tls-cloudfront-storefront", "http-redirect", "public-argocd-blocked", "health-readiness",
          "catalog-config-detail", "order-create-replay-conflict-lookup", "dynamic-no-store-request-id", "static-cache"}


def checkout_payload(product_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", product_id): raise ValueError("Invalid acceptance product ID")
    return dict(items=[dict(productId=product_id, quantity=1)], customer=dict(name="Automation Acceptance", email="acceptance@example.invalid"),
        shippingAddress=dict(line1="100 Acceptance Street", city="Seattle", region="WA", postalCode="98101", countryCode="US"), shippingMethod="standard")


class ProbeFailure(ValueError):
    def __init__(self, check):
        self.check = check
        super().__init__("Public acceptance failed: " + check)


class PublicHTTP:
    def __init__(self, guard, *, resolve=socket.getaddrinfo, connect=socket.create_connection):
        self.guard, self.resolve, self.connect = guard, resolve, connect
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.keylog_filename = None

    def request(self, method, path, *, body=None, headers=None, tls=True):
        if (method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or any(char in path for char in ("\r", "\n", "\\", "#"))
                or len(path) > 512 or body is not None and (not isinstance(body, bytes) or len(body) > 16 * 1024)
                or headers is not None and set(headers) - {"X-Request-Id", "Idempotency-Key", "Content-Type"}):
            raise ValueError("Public probe request escaped its fixed origin contract")
        self.guard()
        port = 443 if tls else 80
        addresses = self.resolve(HOST, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ProbeFailure("public-network-address")
        # Resolve once, validate every answer, then connect to a validated IP.
        # http.client ignores proxy environment variables and never redirects.
        candidates = list(dict.fromkeys(row[4][0] for row in addresses))[:4]
        for index, address in enumerate(candidates):
            self.guard()
            try:
                sock = self.connect((address, port), timeout=10)
                break
            except OSError:
                if index == len(candidates) - 1: raise
        connection = None
        try:
            peer = sock.getpeername()[0]
            if ipaddress.ip_address(peer) != ipaddress.ip_address(address) or not ipaddress.ip_address(peer).is_global:
                raise ProbeFailure("public-peer-address")
            self.guard()
            if tls: sock = self.context.wrap_socket(sock, server_hostname=HOST)
            tls_proof = dict(version=sock.version(), leafSha256=hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()) if tls else None
            if tls and tls_proof["version"] not in ("TLSv1.2", "TLSv1.3"): raise ProbeFailure("tls-version")
            connection = http.client.HTTPConnection(HOST, port=port, timeout=10)
            connection.sock = sock
            self.guard()
            connection.request(method, path, body=body, headers={"Host": HOST, "Accept-Encoding": "identity", **(headers or {})})
            response = connection.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY: raise ProbeFailure("bounded-response")
            pairs = response.getheaders()
            names = [key.lower() for key, _ in pairs]
            if any(names.count(name) > 1 for name in ("x-cache", "x-amz-cf-id", "cache-control", "x-request-id", "location")):
                raise ProbeFailure("ambiguous-response-headers")
            return dict(status=response.status, headers={key.lower(): value for key, value in pairs}, body=data,
                        tls=tls_proof, peer=peer)
        finally:
            if connection is not None: connection.close()
            else: sock.close()


class PublicProbe:
    def __init__(self, guard, session_id, dispatch_id, *, transport=None, dns_verify=None):
        if not re.fullmatch(r"[a-f0-9]{64}", dispatch_id): raise ValueError("An exact public acceptance intent is required")
        self.guard, self.session, self.dispatch_id = guard, session_id, dispatch_id
        self.http = transport or PublicHTTP(guard)
        self.dns_verify = dns_verify
        self.checks, self.observations = [], []
        self.request_index = 0

    def require(self, condition, check):
        if not condition: raise ProbeFailure(check)

    def request(self, method, path, expected, *, payload=None, key=None, api=False, tls=True):
        self.guard()
        self.request_index += 1
        request_id = f"accept-{self.dispatch_id[:24]}-{self.request_index}"
        headers = {"X-Request-Id": request_id} if api else {}
        if payload is not None: headers["Content-Type"] = "application/json"
        if key: headers["Idempotency-Key"] = key
        response = self.http.request(method, path, body=json.dumps(payload, sort_keys=True).encode() if payload is not None else None,
                                     headers=headers, tls=tls)
        self.require(response["status"] == expected, "http-status")
        values = response["headers"]
        if tls:
            self.require(response.get("tls", {}).get("version") in ("TLSv1.2", "TLSv1.3")
                and re.fullmatch(r"[a-f0-9]{64}", response["tls"].get("leafSha256", "")), "tls-proof")
            self.require(bool(values.get("x-amz-cf-id")) and values.get("x-cache") in
                ("Miss from cloudfront", "Hit from cloudfront", "RefreshHit from cloudfront", "Error from cloudfront"), "cloudfront-headers")
        if api:
            self.require("no-store" in values.get("cache-control", "").lower()
                and values.get("x-cache") not in ("Hit from cloudfront", "RefreshHit from cloudfront")
                and values.get("x-request-id") == request_id, "dynamic-no-store-request-id")
            self.require(values.get("content-type", "").lower().startswith("application/json"), "api-content-type")
        self.observations.append(dict(method=method, path=path, status=response["status"],
            cache=values.get("x-cache"), requestId=request_id if api else None, tls=response.get("tls"), peer=response.get("peer")))
        if api:
            try: return json.loads(response["body"])
            except (ValueError, UnicodeError): raise ProbeFailure("api-json") from None
        return response

    def perform(self, target):
        if self.dns_verify is None:
            from edge_activation import EdgeActivation
            EdgeActivation.verify_dns(target, self.guard)
        else:
            self.guard()
            self.dns_verify(target, self.guard)
        self.checks.append("dns")
        page = self.request("GET", "/", 200)
        self.require(page["headers"].get("content-type", "").lower().startswith("text/html"), "storefront-content")
        self.require(b"TechX" in page["body"] and page["headers"].get("x-cache") not in
            ("Hit from cloudfront", "RefreshHit from cloudfront"), "storefront-content")
        self.checks.append("tls-cloudfront-storefront")
        self.guard()
        redirect = self.http.request("GET", "/", tls=False)
        self.require(redirect["status"] in (301, 308) and redirect["headers"].get("location") == f"https://{HOST}/", "http-redirect")
        self.checks.append("http-redirect")
        for path in ("/argocd", "/argocd/", "/argocd/api/version"):
            blocked = self.request("GET", path, 403)
            self.require(b"Forbidden" in blocked["body"], "public-argocd-blocked")
        self.checks.append("public-argocd-blocked")
        for path in ("/healthz", "/readyz"):
            health = self.request("GET", path, 200)
            try: parsed = json.loads(health["body"])
            except (ValueError, UnicodeError): raise ProbeFailure("health-json") from None
            self.require(parsed.get("status") == ("ready" if path == "/readyz" else "ok")
                and parsed.get("service") == "frontend", "health-readiness")
        self.checks.append("health-readiness")
        catalog = self.request("GET", "/api/products", 200, api=True)
        products = catalog.get("products", [])
        self.require(isinstance(products, list) and 1 <= len(products) <= 200, "catalog")
        selected = next((row for row in products if row.get("availability") != "out_of_stock"), None)
        self.require(selected is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", selected.get("id", ""))
            and type(selected.get("priceCents")) is int and selected["priceCents"] > 0, "catalog")
        config = self.request("GET", "/api/store-config", 200, api=True).get("config", {})
        self.require(any(row.get("id") == "standard" for row in config.get("shippingMethods", []))
            and all(type(config.get(field)) is int and config[field] >= 0 for field in ("standardShippingCents", "freeShippingThresholdCents")), "store-config")
        detail = self.request("GET", "/api/products/" + quote(selected["id"], safe=""), 200, api=True)
        self.require(detail.get("product", {}).get("id") == selected["id"], "product-detail")
        self.checks.append("catalog-config-detail")
        payload = checkout_payload(selected["id"])
        key = "acceptance-" + self.dispatch_id
        created = self.request("POST", "/api/orders", 201, payload=payload, key=key, api=True).get("order", {})
        self.require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", created.get("id", "")) and
            all(type(created.get(field)) is int and created[field] >= 0 for field in ("subtotalCents", "shippingCents", "totalCents"))
            and created["subtotalCents"] == selected["priceCents"]
            and created["shippingCents"] == (0 if selected["priceCents"] >= config["freeShippingThresholdCents"] else config["standardShippingCents"])
            and created["totalCents"] == created["subtotalCents"] + created["shippingCents"], "order-create")
        replay = self.request("POST", "/api/orders", 200, payload=payload, key=key, api=True)
        self.require(replay.get("idempotentReplay") is True and replay.get("order") == created, "order-replay")
        changed = deepcopy(payload)
        changed["items"][0]["quantity"] = 2
        conflict = self.request("POST", "/api/orders", 409, payload=changed, key=key, api=True)
        self.require(conflict.get("error", {}).get("code") == "IDEMPOTENCY_CONFLICT"
            and conflict["error"].get("requestId") == self.observations[-1]["requestId"], "order-conflict")
        lookup = self.request("GET", "/api/orders/" + created["id"], 200, api=True)
        self.require(lookup.get("order") == created, "order-lookup")
        self.checks.extend(("order-create-replay-conflict-lookup", "dynamic-no-store-request-id"))
        assets = re.findall(rb'(?:src|href)=["\'](/_next/static/[A-Za-z0-9_./-]+\.(?:js|css))["\']', page["body"])
        self.require(bool(assets), "static-asset-discovery")
        path = assets[0].decode("ascii")
        self.require(".." not in path.split("/"), "static-asset-path")
        hit = False
        for _ in range(4):
            asset = self.request("GET", path, 200)
            cache_control = asset["headers"].get("cache-control", "").lower()
            self.require("immutable" in cache_control and re.search(r"(?:^|[,\s])max-age=31536000(?:[,\s]|$)", cache_control), "static-cache-policy")
            if asset["headers"].get("x-cache") in ("Hit from cloudfront", "RefreshHit from cloudfront"):
                hit = True
                break
        self.require(hit, "static-cache-hit")
        self.checks.append("static-cache")
        self.guard()
        self.require(set(self.checks) == CHECKS, "complete-public-suite")
        return dict(status="passed", networkView="public", checks=sorted(self.checks), observations=self.observations,
                    private="pending", vpn="pending", orderSummary=dict(id=created["id"], productId=selected["id"],
                        subtotalCents=created["subtotalCents"], shippingCents=created["shippingCents"], totalCents=created["totalCents"]))
