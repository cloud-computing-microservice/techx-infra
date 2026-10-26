"""A real, bounded Client VPN session from the separate deployment-control VPC.

Only an AWS-observed connection plus matching TUN address/routes proves VPN
readiness. Logs and profiles are private; credentials never enter task output.
"""
import base64
from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

import dns.resolver

from bootstrap_assets import read_existing_secret, validate_secret
from cloudflare_dns import HOST

NETWORK = ipaddress.ip_network("10.52.0.0/16")
CLIENTS = ipaddress.ip_network("172.24.0.0/22")
OPERATOR = "techx-demo-operator"
STATES = {"active", "terminating", "terminated", "failed-to-terminate"}


class VpnFailure(ValueError): pass


def certificate_der(pem):
    match = re.fullmatch(r"\s*-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\r\n]+)-----END CERTIFICATE-----\s*", pem)
    if not match: raise VpnFailure("VPN certificate is not one bounded PEM certificate")
    return base64.b64decode(re.sub(r"\s", "", match[1]), validate=True)


def controlled_profile(exported, endpoint, material, device):
    validate_secret("vpn-pki", material)
    if (not isinstance(exported, str) or len(exported) > 48 * 1024 or not re.fullmatch(r"tx[a-f0-9]{10}", device)
            or not re.fullmatch(r"cvpn-endpoint-[a-f0-9]{8,17}", endpoint)):
        raise VpnFailure("Invalid bounded VPN profile/device")
    blocks = re.findall(r"(?s)<ca>\s*(.*?)\s*</ca>", exported)
    if len(blocks) != 1 or certificate_der(blocks[0]) != certificate_der(material["ca.crt"]):
        raise VpnFailure("Exported VPN trust chain differs from the owned session PKI")
    text = re.sub(r"(?s)<ca>.*?</ca>", "", exported)
    allowed = {"client", "dev tun", "proto udp", "remote-random-hostname", "resolv-retry infinite", "nobind",
        "persist-key", "persist-tun", "remote-cert-tls server", "cipher AES-256-GCM", "cipher AES-256-CBC", "verb 3",
        "reneg-sec 0", "auth SHA256", "auth SHA1", "verify-x509-name server.shop.dinhminhkhoa.id.vn name"}
    remotes, directives = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")): continue
        directives.add(line)
        remote = re.fullmatch(r"remote (" + re.escape(endpoint) + r"(?:-[a-z0-9]{1,64})?(?:\.prod)?\.clientvpn\.us-east-1\.amazonaws\.com) 443", line)
        if remote: remotes.append(remote[1])
        elif line not in allowed: raise VpnFailure("VPN export contains an unexpected directive")
    if len(remotes) != 1 or not {"client", "dev tun", "proto udp", "remote-cert-tls server"} <= directives:
        raise VpnFailure("VPN export does not describe the exact mutual-TLS UDP client")
    # Ignore all exported routing/persistence/retry options. Server-pushed
    # routes and DNS are disabled; only the owned workload CIDR is installed.
    lines = ["client", f"dev {device}", "dev-type tun", "proto udp", f"remote {remotes[0]} 443", "remote-random-hostname",
        "nobind", "remote-cert-tls server", "verify-x509-name server.shop.dinhminhkhoa.id.vn name", "tls-version-min 1.2",
        "data-ciphers AES-256-GCM:AES-128-GCM", "route-nopull", "route 10.52.0.0 255.255.0.0", "script-security 1",
        "resolv-retry 5", "connect-retry-max 1", "server-poll-timeout 20", "remap-usr1 SIGTERM", "auth-nocache", "verb 3"]
    for tag, key in (("ca", "ca.crt"), ("cert", "operator.crt"), ("key", "operator.key")):
        lines += [f"<{tag}>", material[key].strip(), f"</{tag}>"]
    return "\n".join(lines) + "\n"


class VpnTunnel:
    def __init__(self, runtime, guard, binding, task_id, *, sleep=time.sleep, popen=subprocess.Popen, resolver=None):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id): raise VpnFailure("An exact private task identity is required")
        self.runtime, self.guard, self.binding = runtime, guard, binding
        self.sleep, self.popen = sleep, popen
        self.device, self.process, self.connection = "tx" + task_id[:10], None, None
        self.resolver = resolver or dns.resolver.Resolver(configure=False)
        self.resolver.nameservers, self.resolver.timeout, self.resolver.lifetime = ["10.52.0.2"], 3, 10
        self.disconnected = False

    def command(self, args):
        self.guard()
        result = self.runtime.run(args, env=self.runtime.env, check=True, capture_output=True, timeout=20)
        if len(result.stdout) > 256 * 1024: raise VpnFailure("Oversized VPN route/tool observation")
        return result.stdout

    def connections(self, connection_id=None):
        self.guard()
        response = self.runtime.clients["ec2"].describe_client_vpn_connections(ClientVpnEndpointId=self.binding["endpointId"],
            MaxResults=1000, **({"Filters": [dict(Name="connection-id", Values=[connection_id])]} if connection_id else {}))
        rows = response.get("Connections")
        if not isinstance(rows, list) or response.get("NextToken") or len(rows) > 1000:
            raise VpnFailure("Client VPN connection inventory is incomplete")
        seen = set()
        for row in rows:
            if (row.get("ClientVpnEndpointId") != self.binding["endpointId"] or row.get("Status", {}).get("Code") not in STATES
                    or not re.fullmatch(r"cvpn-connection-[a-f0-9]{8,17}", row.get("ConnectionId", ""))
                    or row["ConnectionId"] in seen):
                raise VpnFailure("Client VPN connection identity/status is ambiguous")
            seen.add(row["ConnectionId"])
        return rows

    def route(self, address):
        if ipaddress.ip_address(address) not in NETWORK: raise VpnFailure("Private destination is outside staging")
        rows = json.loads(self.command(["ip", "-j", "route", "get", address]))
        if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("dev") != self.device
                or rows[0].get("prefsrc", rows[0].get("src")) != self.connection["ClientIp"]):
            raise VpnFailure("Private traffic does not use the observed Client VPN tunnel/address")
        return dict(destination=address, device=self.device, source=self.connection["ClientIp"])

    def assert_connected(self):
        self.guard()
        if self.process is None or self.process.poll() is not None or self.connection is None:
            raise VpnFailure("The owned VPN process/connection is no longer active")
        rows = self.connections(self.connection["ConnectionId"])
        if (len(rows) != 1 or rows[0].get("CommonName") != OPERATOR or rows[0].get("ClientIp") != self.connection["ClientIp"]
                or rows[0]["Status"]["Code"] != "active"):
            raise VpnFailure("AWS no longer observes this exact active VPN connection")
        self.route("10.52.0.2")

    def private_address(self):
        self.assert_connected()
        sets = []
        for name in (HOST, self.binding["originDomainName"]):
            self.guard()
            addresses = {str(answer) for answer in self.resolver.resolve(name, "A", search=False)}
            if not addresses or any(ipaddress.ip_address(address) not in NETWORK for address in addresses):
                raise VpnFailure("Private DNS returned no owned workload address")
            sets.append(addresses)
        common = sets[0] & sets[1]
        if not common: raise VpnFailure("Private storefront DNS does not resolve to the admitted ALB")
        address = sorted(common)[0]
        self.route(address)
        return address

    def stop_local(self):
        # Local process ownership is independent of the remote lease. Always
        # terminate only the process we spawned, including after cleanup fencing.
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)

    def disconnect(self):
        self.stop_local()
        if self.connection is None: return
        identifier = self.connection["ConnectionId"]
        rows = self.connections(identifier)
        if len(rows) != 1: raise VpnFailure("VPN disconnect lacks positive connection evidence")
        if rows[0]["Status"]["Code"] == "active":
            self.guard()
            try:
                self.runtime.clients["ec2"].terminate_client_vpn_connections(ClientVpnEndpointId=self.binding["endpointId"], ConnectionId=identifier)
            except Exception:
                observed = self.connections(identifier)
                if len(observed) != 1 or observed[0]["Status"]["Code"] != "terminated": raise
        for _ in range(60):
            rows = self.connections(identifier)
            if len(rows) != 1: raise VpnFailure("Empty connection read cannot prove VPN disconnect")
            if rows[0]["Status"]["Code"] == "terminated":
                links = json.loads(self.command(["ip", "-j", "link", "show"]))
                if any(row.get("ifname") == self.device for row in links): raise VpnFailure("VPN device remains after disconnect")
                self.disconnected = True
                return
            if rows[0]["Status"]["Code"] == "failed-to-terminate": raise VpnFailure("AWS failed to terminate the owned VPN connection")
            self.sleep(2)
        raise TimeoutError("Owned VPN connection was not observed terminated")

    def evidence(self):
        if self.connection is None: raise VpnFailure("No positively observed VPN connection")
        return dict(endpointId=self.binding["endpointId"], connectionId=self.connection["ConnectionId"],
            clientIp=self.connection["ClientIp"], commonName=OPERATOR, device=self.device, disconnected=self.disconnected)

    @contextmanager
    def opened(self, *, observations=90):
        if type(observations) is not int or not 1 <= observations <= 90: raise VpnFailure("VPN readiness wait must be bounded")
        self.guard()
        if not self.command(["/usr/sbin/openvpn", "--version"]).startswith(b"OpenVPN 2.6."):
            raise VpnFailure("The reviewed Ubuntu OpenVPN 2.6 toolchain is required")
        before = self.connections()
        if any(row.get("CommonName") == OPERATOR and row["Status"]["Code"] != "terminated" for row in before):
            raise VpnFailure("Another operator VPN connection is active; refusing takeover")
        prior_ids = {row["ConnectionId"] for row in before}
        material = read_existing_secret(self.runtime, "vpn-pki", self.guard)
        self.guard()
        exported = self.runtime.clients["ec2"].export_client_vpn_client_configuration(ClientVpnEndpointId=self.binding["endpointId"])["ClientConfiguration"]
        profile = controlled_profile(exported, self.binding["endpointId"], material, self.device)
        with tempfile.TemporaryDirectory(prefix="techx-private-vpn-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            filename = root / "client.private.ovpn"
            filename.write_text(profile, encoding="ascii")
            filename.chmod(0o600)
            log = root / "vpn.private.log"
            with log.open("xb") as output:
                log.chmod(0o600)
                self.guard()
                self.process = self.popen(["/usr/sbin/openvpn", "--config", str(filename)], env=self.runtime.env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                failed = False
                try:
                    for _ in range(observations):
                        self.guard()
                        if self.process.poll() is not None: raise VpnFailure("OpenVPN exited before readiness")
                        rows = [row for row in self.connections() if row.get("CommonName") == OPERATOR
                                and row["ConnectionId"] not in prior_ids and row["Status"]["Code"] == "active"]
                        if len(rows) > 1: raise VpnFailure("Multiple new operator VPN connections are ambiguous")
                        if rows:
                            if ipaddress.ip_address(rows[0].get("ClientIp", "")) not in CLIENTS:
                                raise VpnFailure("VPN client address differs from the owned pool")
                            self.connection = rows[0]
                            addresses = [row for row in json.loads(self.command(["ip", "-j", "address", "show"])) if row.get("ifname") == self.device]
                            if len(addresses) == 1 and any(item.get("family") == "inet" and item.get("local") == rows[0]["ClientIp"]
                                    for item in addresses[0].get("addr_info", [])):
                                self.connection = rows[0]
                                self.assert_connected()
                                break
                        self.sleep(2)
                    else: raise TimeoutError("Actual Client VPN readiness was not proven")
                    yield self
                except BaseException:
                    failed = True
                    raise
                finally:
                    self.stop_local()
                    # On a fenced session this guard refuses AWS mutation; the
                    # independent cleanup owns connection/endpoint teardown.
                    try: self.disconnect()
                    except Exception:
                        if not failed: raise
