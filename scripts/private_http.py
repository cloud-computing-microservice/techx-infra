"""Verified HTTPS over the positively observed Client VPN route only."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl

from cloudflare_dns import HOST


class PrivateFailure(ValueError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__("Private acceptance failed: " + classification)


class PrivateHTTP:
    def __init__(self, tunnel, guard, *, connect=socket.create_connection):
        self.tunnel, self.guard, self.connect = tunnel, guard, connect
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.keylog_filename = None
        self.observations = []

    def request(self, method, path, *, payload=None, token=None, idempotency_key=None):
        routes = {("POST", "/argocd/api/v1/session"), ("GET", "/argocd/api/v1/session/userinfo"),
                  ("GET", "/argocd/api/v1/applications/techx-staging"), ("POST", "/api/orders")}
        if ((method, path) not in routes and not (method == "GET" and re.fullmatch(r"/api/orders/ord_[a-f0-9-]{8,64}", path))
                or token is not None and not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
                or token is not None and (len(token) > 8192 or not path.startswith("/argocd/"))
                or idempotency_key is not None and (method, path) != ("POST", "/api/orders")):
            raise PrivateFailure("request-contract")
        body = json.dumps(payload, sort_keys=True).encode() if payload is not None else None
        if body is not None and len(body) > 16 * 1024: raise PrivateFailure("bounded-request")
        self.guard()
        address = self.tunnel.private_address()
        if (ipaddress.ip_address(address) not in ipaddress.ip_network("10.52.0.0/16")
                or ipaddress.ip_address(self.tunnel.connection["ClientIp"]) not in ipaddress.ip_network("172.24.0.0/22")):
            raise PrivateFailure("vpn-address-scope")
        sock, connection = None, None
        try:
            self.guard()
            sock = self.connect((address, 443), timeout=10)
            if sock.getpeername()[0] != address or sock.getsockname()[0] != self.tunnel.connection["ClientIp"]:
                raise PrivateFailure("vpn-socket-route")
            self.guard()
            sock = self.context.wrap_socket(sock, server_hostname=HOST)
            if sock.version() not in ("TLSv1.2", "TLSv1.3"): raise PrivateFailure("private-tls")
            leaf = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            connection = http.client.HTTPConnection(HOST, port=443, timeout=10)
            connection.sock = sock
            headers = {"Host": HOST, "Accept": "application/json", "Accept-Encoding": "identity"}
            if body is not None: headers["Content-Type"] = "application/json"
            if token: headers["Authorization"] = "Bearer " + token
            if idempotency_key: headers["Idempotency-Key"] = idempotency_key
            self.tunnel.assert_connected()
            self.guard()
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024: raise PrivateFailure("bounded-response")
            if 300 <= response.status < 400: raise PrivateFailure("private-redirect")
            self.observations.append(dict(method=method, path=path, status=response.status, peer=address, leafSha256=leaf,
                clientIp=self.tunnel.connection["ClientIp"], device=self.tunnel.device))
            try: value = json.loads(data)
            except (ValueError, UnicodeError): raise PrivateFailure("private-json") from None
            return response.status, value
        except ssl.SSLError:
            raise PrivateFailure("private-tls") from None
        except OSError:
            raise PrivateFailure("private-network") from None
        finally:
            if connection is not None: connection.close()
            elif sock is not None: sock.close()
