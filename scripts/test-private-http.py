import json
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cloudflare_dns import HOST
from private_http import PrivateFailure, PrivateHTTP


class PrivateTransportTests(unittest.TestCase):
    def setUp(self):
        self.tunnel = SimpleNamespace(private_address=lambda: "10.52.10.5", connection=dict(ClientIp="172.24.0.10"), device="txaaaaaaaaaa", assert_connected=Mock())
        self.guard, self.sock, self.connection = Mock(), Mock(), Mock()
        self.sock.getpeername.return_value = ("10.52.10.5", 443)
        self.sock.getsockname.return_value = ("172.24.0.10", 12345)
        self.sock.version.return_value = "TLSv1.3"
        self.sock.getpeercert.return_value = b"leaf"
        self.http = PrivateHTTP(self.tunnel, self.guard, connect=Mock(return_value=self.sock))
        self.response = Mock(status=200)
        self.response.read.return_value = json.dumps(dict(token="header.payload.signature")).encode()
        self.connection.getresponse.return_value = self.response

    def request(self):
        with patch.object(self.http.context, "wrap_socket", return_value=self.sock) as wrap, \
                patch("private_http.http.client.HTTPConnection", return_value=self.connection):
            result = self.http.request("POST", "/argocd/api/v1/session", payload=dict(username="admin", password="private-fixture"))
            wrap.assert_called_once_with(self.sock, server_hostname=HOST)
            return result

    def test_verified_private_peer_and_tunnel_source_are_required_without_logging_credentials(self):
        self.assertTrue(self.http.context.check_hostname)
        self.assertEqual(self.http.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(self.request()[0], 200)
        self.http.connect.assert_called_once_with(("10.52.10.5", 443), timeout=10)
        self.assertNotIn("private-fixture", json.dumps(self.http.observations))
        self.assertNotIn("header.payload.signature", json.dumps(self.http.observations))
        self.tunnel.assert_connected.assert_called_once()

    def test_unexpected_peer_or_local_source_cannot_receive_private_credentials(self):
        for source in (True, False):
            self.sock.getpeername.return_value = ("10.52.10.5" if source else "8.8.8.8", 443)
            self.sock.getsockname.return_value = ("172.30.0.5" if source else "172.24.0.10", 12345)
            with self.subTest(source=source), self.assertRaises(PrivateFailure): self.request()
        self.connection.request.assert_not_called()

    def test_certificate_failure_is_distinct_from_network_failure(self):
        with patch.object(self.http.context, "wrap_socket", side_effect=ssl.SSLCertVerificationError("invalid hostname")):
            with self.assertRaisesRegex(PrivateFailure, "private-tls"):
                self.http.request("GET", "/argocd/api/v1/session/userinfo")
        self.http.connect.side_effect = TimeoutError("network timeout")
        with self.assertRaisesRegex(PrivateFailure, "private-network"):
            self.http.request("GET", "/argocd/api/v1/session/userinfo")

    def test_redirect_is_not_followed(self):
        self.response.status = 302
        with self.assertRaisesRegex(PrivateFailure, "private-redirect"): self.request()
        self.assertEqual(self.connection.request.call_count, 1)

    def test_private_token_cannot_be_sent_to_an_arbitrary_path_or_frontend_api(self):
        for path in ("https://other.example/", "/api/orders", "/argocd/../../other"):
            with self.subTest(path=path), self.assertRaises(PrivateFailure):
                self.http.request("POST", path, token="header.payload.signature")
        self.http.connect.assert_not_called()


if __name__ == "__main__": unittest.main()
