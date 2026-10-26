from copy import deepcopy
import json
import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from cloudflare_dns import HOST
from public_probe import CHECKS, MAX_BODY, ProbeFailure, PublicHTTP, PublicProbe


class Storefront:
    """In-memory HTTP service, including independent idempotent order behavior."""
    def __init__(self):
        self.calls, self.orders = [], {}
        self.mutate = lambda response, method, path: None
        self.price = 2500
        self.asset_reads = 0

    def request(self, method, path, *, body=None, headers=None, tls=True):
        self.calls.append((method, path, body, deepcopy(headers), tls))
        response = dict(status=200, headers={"x-cache": "Miss from cloudfront", "x-amz-cf-id": "cf-request-id",
            "content-type": "application/json"}, tls=dict(version="TLSv1.3", leafSha256="a" * 64), peer="8.8.8.8")
        if not tls:
            response.update(status=301, body=b"", headers={"location": f"https://{HOST}/"})
        elif path == "/":
            response["headers"]["content-type"] = "text/html"
            response["body"] = b'<html>TechX<script src="/_next/static/chunks/storefront.js"></script></html>'
        elif path.startswith("/argocd"):
            response.update(status=403, body=b"Forbidden")
        elif path in ("/healthz", "/readyz"):
            response["body"] = json.dumps(dict(status="ok" if path == "/healthz" else "ready", service="frontend")).encode()
        elif path.startswith("/_next/static/"):
            self.asset_reads += 1
            response["headers"].update({"cache-control": "public, max-age=31536000, immutable",
                "x-cache": "Hit from cloudfront" if self.asset_reads > 1 else "Miss from cloudfront"})
            response["body"] = b"console.log('storefront');"
        else:
            response["headers"].update({"cache-control": "no-store", "x-request-id": headers.get("X-Request-Id")})
            product = dict(id="scope-1", availability="in_stock", priceCents=self.price)
            if path == "/api/products": value = dict(products=[product])
            elif path == "/api/products/scope-1": value = dict(product=product)
            elif path == "/api/store-config": value = dict(config=dict(shippingMethods=[dict(id="standard")], standardShippingCents=999, freeShippingThresholdCents=10000))
            elif path == "/api/orders" and method == "POST":
                payload = json.loads(body)
                key = headers["Idempotency-Key"]
                if key not in self.orders:
                    order = dict(id="ord_12345678-abcd", subtotalCents=self.price, shippingCents=999, totalCents=self.price + 999)
                    self.orders[key] = payload, order
                    response["status"], value = 201, dict(order=order)
                elif self.orders[key][0] == payload:
                    response["status"], value = 200, dict(order=self.orders[key][1], idempotentReplay=True)
                else: response["status"], value = 409, dict(error=dict(code="IDEMPOTENCY_CONFLICT", requestId=headers.get("X-Request-Id")))
            elif path == "/api/orders/ord_12345678-abcd": value = dict(order=next(iter(self.orders.values()))[1])
            else: raise AssertionError("Unexpected public request")
            response["body"] = json.dumps(value).encode()
        self.mutate(response, method, path)
        return response


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.service = Storefront()
        self.guard = Mock()
        self.dns = Mock()
        self.probe = PublicProbe(self.guard, "staging-test123", "b" * 64, transport=self.service, dns_verify=self.dns)

    def test_full_suite_tests_behavior_and_keeps_private_vpn_pending(self):
        result = self.probe.perform("d123.cloudfront.net")
        self.assertEqual(result["checks"], sorted(CHECKS))
        self.assertEqual((result["private"], result["vpn"]), ("pending", "pending"))
        self.dns.assert_called_once_with("d123.cloudfront.net", self.guard)
        writes = [row for row in self.service.calls if row[0] == "POST"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0][2], writes[1][2])
        self.assertNotEqual(writes[1][2], writes[2][2])
        self.assertEqual(len({row[3]["Idempotency-Key"] for row in writes}), 1)
        self.assertEqual(len(self.service.orders), 1)
        self.assertEqual(self.service.asset_reads, 2)

    def test_failed_status_tls_origin_or_admin_exposure_never_passes(self):
        for failure in ("status", "tls", "cloudfront", "admin"):
            def mutate(response, method, path):
                if path == "/" and failure == "status": response["status"] = 503
                if path == "/" and failure == "tls": response["tls"]["version"] = "TLSv1"
                if path == "/" and failure == "cloudfront": response["headers"].pop("x-amz-cf-id")
                if path == "/argocd" and failure == "admin": response["status"] = 200
            self.service.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(ProbeFailure): self.probe.perform("d123.cloudfront.net")
        self.assertFalse(self.service.orders)

    def test_api_cache_or_request_id_mismatch_is_rejected(self):
        for name, value in (("cache-control", "public, max-age=60"), ("x-cache", "Hit from cloudfront"), ("x-request-id", "other")):
            self.service.mutate = lambda response, method, path: response["headers"].update({name: value}) if path == "/api/products" else None
            with self.subTest(header=name), self.assertRaisesRegex(ProbeFailure, "dynamic-no-store-request-id"):
                self.probe.perform("d123.cloudfront.net")

    def test_replay_must_return_the_same_order_and_true_replay_flag(self):
        def mutate(response, method, path):
            if path == "/api/orders" and response["status"] == 200:
                value = json.loads(response["body"])
                value["order"]["id"] = "ord_other"
                response["body"] = json.dumps(value).encode()
        self.service.mutate = mutate
        with self.assertRaisesRegex(ProbeFailure, "order-replay"): self.probe.perform("d123.cloudfront.net")

    def test_static_cache_requires_policy_and_an_observed_hit_with_bounded_requests(self):
        self.service.mutate = lambda response, method, path: response["headers"].update({"x-cache": "Miss from cloudfront"}) if path.startswith("/_next") else None
        with self.assertRaisesRegex(ProbeFailure, "static-cache-hit"): self.probe.perform("d123.cloudfront.net")
        self.assertEqual(self.service.asset_reads, 4)

    def test_dns_or_lease_failure_prevents_public_requests(self):
        self.dns.side_effect = TimeoutError("DNS not available")
        with self.assertRaises(TimeoutError): self.probe.perform("d123.cloudfront.net")
        self.assertFalse(self.service.calls)
        self.dns.side_effect = None
        self.guard.side_effect = ValueError("cleanup won")
        with self.assertRaises(ValueError): self.probe.perform("d123.cloudfront.net")
        self.assertFalse(self.service.calls)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.connect = Mock()
        self.guard = Mock()
        self.resolve = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        self.http = PublicHTTP(self.guard, resolve=self.resolve, connect=self.connect)

    def test_any_private_dns_answer_rejects_before_connection(self):
        for address in ("10.52.1.2", "127.0.0.1", "169.254.169.254", "::1"):
            self.resolve.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)))
            with self.subTest(address=address), self.assertRaises(ProbeFailure): self.http.request("GET", "/")
            self.resolve.return_value.pop()
        self.connect.assert_not_called()

    def test_transport_uses_validated_ip_sni_and_certificate_verification_without_redirects(self):
        sock = Mock()
        sock.getpeername.return_value = ("8.8.8.8", 443)
        sock.version.return_value = "TLSv1.3"
        sock.getpeercert.return_value = b"leaf certificate"
        self.connect.return_value = sock
        response = Mock(status=302)
        response.read.return_value = b"redirect"
        response.getheaders.return_value = [("location", "https://other.example/")]
        connection = Mock()
        connection.getresponse.return_value = response
        self.assertTrue(self.http.context.check_hostname)
        self.assertEqual(self.http.context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(self.http.context, "wrap_socket", return_value=sock) as wrap, \
                patch("public_probe.http.client.HTTPConnection", return_value=connection):
            result = self.http.request("GET", "/")
        self.assertEqual(result["status"], 302)
        self.connect.assert_called_once_with(("8.8.8.8", 443), timeout=10)
        wrap.assert_called_once_with(sock, server_hostname=HOST)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_tls_verification_error_is_propagated_and_socket_closed(self):
        sock = Mock()
        sock.getpeername.return_value = ("8.8.8.8", 443)
        self.connect.return_value = sock
        with patch.object(self.http.context, "wrap_socket", side_effect=ssl.SSLCertVerificationError("wrong hostname")):
            with self.assertRaises(ssl.SSLCertVerificationError): self.http.request("GET", "/")
        sock.close.assert_called_once()

    def test_caller_cannot_change_origin_or_send_authorization(self):
        for path in ("https://other.example/", "//other.example/", "/\r\nInjected: bad"):
            with self.subTest(path=path), self.assertRaises(ValueError): self.http.request("GET", path)
        with self.assertRaises(ValueError): self.http.request("GET", "/", headers={"Authorization": "not-allowed"})
        self.connect.assert_not_called()

    def test_oversized_body_and_duplicate_cache_headers_are_rejected(self):
        sock = Mock()
        sock.getpeername.return_value = ("8.8.8.8", 443)
        sock.version.return_value = "TLSv1.3"
        sock.getpeercert.return_value = b"leaf"
        self.connect.return_value = sock
        response, connection = Mock(status=200), Mock()
        connection.getresponse.return_value = response
        for oversized in (True, False):
            response.read.return_value = b"x" * (MAX_BODY + 1) if oversized else b"ok"
            response.getheaders.return_value = [("x-cache", "Miss from cloudfront"), ("X-Cache", "Hit from cloudfront")]
            with self.subTest(oversized=oversized), patch.object(self.http.context, "wrap_socket", return_value=sock), \
                    patch("public_probe.http.client.HTTPConnection", return_value=connection):
                with self.assertRaises(ProbeFailure): self.http.request("GET", "/")
        self.assertEqual(connection.close.call_count, 2)

    def test_peer_address_change_is_refused_before_tls_or_http(self):
        sock = Mock()
        sock.getpeername.return_value = ("10.52.1.2", 443)
        self.connect.return_value = sock
        with patch.object(self.http.context, "wrap_socket") as wrap:
            with self.assertRaises(ProbeFailure): self.http.request("GET", "/")
            wrap.assert_not_called()
        sock.close.assert_called_once()

    def test_unreachable_address_can_fail_over_before_any_http_request(self):
        sock = Mock()
        sock.getpeername.return_value = ("8.8.8.8", 443)
        sock.version.return_value = "TLSv1.3"
        sock.getpeercert.return_value = b"leaf"
        self.resolve.return_value.insert(0, (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)))
        self.connect.side_effect = [OSError("IPv6 route unavailable"), sock]
        response, connection = Mock(status=200), Mock()
        response.read.return_value = b"ok"
        response.getheaders.return_value = []
        connection.getresponse.return_value = response
        with patch.object(self.http.context, "wrap_socket", return_value=sock), \
                patch("public_probe.http.client.HTTPConnection", return_value=connection):
            self.http.request("POST", "/api/orders", body=b"{}")
        self.assertEqual(self.connect.call_count, 2)
        self.assertEqual(connection.request.call_count, 1)


if __name__ == "__main__": unittest.main()
