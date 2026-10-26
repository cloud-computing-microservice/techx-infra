from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

from bootstrap_assets import read_existing_secret
from vpn_probe import OPERATOR, VpnFailure, VpnTunnel, controlled_profile

spec = importlib.util.spec_from_file_location("vpn_asset_fixture", Path(__file__).with_name("test-bootstrap-assets.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class VpnTests(unittest.TestCase):
    def setUp(self):
        self.assets = support.AssetTests()
        self.assets.setUp()
        self.addCleanup(self.assets.doCleanups)
        self.assets.bootstrap.secret("vpn-pki")
        self.runtime = self.assets.runtime
        describe_secret = self.runtime.clients["secretsmanager"].describe_secret
        def describe_reference(**kw):
            self.assets.shape("secretsmanager", "DescribeSecret", kw)
            identifier = kw["SecretId"]
            name = next((name for name, item in self.assets.secrets.items() if item["ARN"] == identifier), identifier)
            return describe_secret(SecretId=name)
        self.runtime.clients["secretsmanager"].describe_secret = describe_reference
        self.runtime.env = {"PATH": "/usr/sbin:/usr/bin"}
        self.endpoint = "cvpn-endpoint-1234567890abcdef0"
        self.binding = dict(endpointId=self.endpoint, originDomainName="internal-techx.us-east-1.elb.amazonaws.com")
        self.export = "\n".join(("client", "dev tun", "proto udp", f"remote {self.endpoint}.prod.clientvpn.us-east-1.amazonaws.com 443",
            "remote-random-hostname", "resolv-retry infinite", "nobind", "persist-key", "persist-tun", "remote-cert-tls server", "cipher AES-256-GCM",
            "verb 3", "<ca>", self.assets.material["ca.crt"].strip(), "</ca>"))
        self.rows, self.process, self.device_up = [], None, False
        self.terminations, self.network_calls, self.commands, self.guards = [], [], [], 0
        self.lost_terminate = self.hide_terminate = self.guard_lost = self.bad_route = self.wrong_address = False
        self.connection = dict(ClientVpnEndpointId=self.endpoint, ConnectionId="cvpn-connection-1234567890abcdef0",
            CommonName=OPERATOR, ClientIp="172.24.0.10", Status=dict(Code="active"))
        def describe(**kw):
            self.assets.shape("ec2", "DescribeClientVpnConnections", kw)
            self.network_calls.append("describe")
            rows = self.rows
            if kw.get("Filters"): rows = [row for row in rows if row["ConnectionId"] == kw["Filters"][0]["Values"][0]]
            return dict(Connections=deepcopy(rows))
        def export(**kw):
            self.assets.shape("ec2", "ExportClientVpnClientConfiguration", kw)
            self.network_calls.append("export")
            return dict(ClientConfiguration=self.export)
        def terminate(**kw):
            self.assets.shape("ec2", "TerminateClientVpnConnections", kw)
            self.terminations.append(kw)
            self.assertEqual(kw, dict(ClientVpnEndpointId=self.endpoint, ConnectionId=self.connection["ConnectionId"]))
            self.connection["Status"]["Code"] = "terminated"
            if self.hide_terminate: self.rows.clear()
            if self.lost_terminate: raise TimeoutError("termination response lost")
            return dict(ClientVpnEndpointId=self.endpoint)
        self.runtime.clients["ec2"] = SimpleNamespace(describe_client_vpn_connections=describe,
            export_client_vpn_client_configuration=export, terminate_client_vpn_connections=terminate)
        def run(command, **kw):
            self.commands.append(command)
            if command == ["/usr/sbin/openvpn", "--version"]: payload = b"OpenVPN 2.6.14 x86_64\n"
            elif command[:4] == ["ip", "-j", "address", "show"]:
                payload = json.dumps([dict(ifname=self.tunnel.device, addr_info=[dict(family="inet", local="172.24.0.99" if self.wrong_address else "172.24.0.10")])]
                    if self.device_up else []).encode()
            elif command[:4] == ["ip", "-j", "route", "get"]:
                payload = json.dumps([dict(dev="eth0" if self.bad_route else self.tunnel.device, prefsrc="172.24.0.10")]).encode()
            elif command == ["ip", "-j", "link", "show"]:
                payload = json.dumps([dict(ifname=self.tunnel.device)] if self.device_up else []).encode()
            else: raise AssertionError(command)
            return SimpleNamespace(stdout=payload)
        self.runtime.run = run
        def stop():
            self.device_up = False
            self.process.returncode = 0
        def popen(command, **kw):
            self.assertEqual(command[:2], ["/usr/sbin/openvpn", "--config"])
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.profile_file = Path(command[2])
            self.assertTrue(self.profile_file.is_file())
            self.device_up = True
            self.rows.append(self.connection)
            self.process = SimpleNamespace(returncode=None, poll=lambda: self.process.returncode, terminate=stop, kill=stop, wait=lambda **kw: 0)
            return self.process
        def guard():
            self.guards += 1
            if self.guard_lost: raise ValueError("lease fenced")
        resolver = SimpleNamespace(resolve=lambda name, kind, **kw: ["10.52.10.5"])
        self.tunnel = VpnTunnel(self.runtime, guard, self.binding, "a" * 32, sleep=lambda _: None, popen=popen, resolver=resolver)

    def test_real_aws_connection_route_dns_and_positive_disconnect_are_required(self):
        with self.tunnel.opened(observations=1) as tunnel:
            self.assertEqual(tunnel.private_address(), "10.52.10.5")
            self.assertFalse(tunnel.disconnected)
            self.assertEqual(tunnel.evidence()["connectionId"], self.connection["ConnectionId"])
        self.assertTrue(self.tunnel.disconnected)
        self.assertEqual(len(self.terminations), 1)
        self.assertEqual(self.assets.secret_creates, 1)
        self.assertFalse(self.profile_file.exists())

    def test_log_or_process_alone_cannot_pass_without_matching_tun_address(self):
        self.wrong_address = True
        with self.assertRaises(TimeoutError):
            with self.tunnel.opened(observations=1): self.fail("wrong tunnel address accepted")
        self.assertEqual(self.process.returncode, 0)
        self.assertEqual(len(self.terminations), 1)

    def test_private_route_must_use_the_observed_vpn_device(self):
        self.bad_route = True
        with self.assertRaises(VpnFailure):
            with self.tunnel.opened(observations=1): self.fail("route bypassed VPN")
        self.assertEqual(self.process.returncode, 0)

    def test_existing_operator_connection_is_never_disconnected_or_reused(self):
        existing = deepcopy(self.connection)
        self.rows.append(existing)
        with self.assertRaises(VpnFailure):
            with self.tunnel.opened(observations=1): self.fail("existing client reused")
        self.assertIsNone(self.process)
        self.assertFalse(self.terminations)

    def test_export_cannot_inject_scripts_routes_foreign_remote_or_credentials(self):
        for suffix in ("\nup /tmp/script", "\nroute 0.0.0.0 0.0.0.0", "\n<key>bad</key>",
                f"\nremote other.{self.endpoint}.prod.clientvpn.us-east-1.amazonaws.com 443"):
            with self.subTest(suffix=suffix), self.assertRaises(VpnFailure):
                controlled_profile(self.export + suffix, self.endpoint, self.assets.material, self.tunnel.device)

    def test_profile_keeps_only_owned_route_and_bounds_reconnects(self):
        profile = controlled_profile(self.export, self.endpoint, self.assets.material, self.tunnel.device)
        self.assertIn("route-nopull\nroute 10.52.0.0 255.255.0.0", profile)
        self.assertIn("verify-x509-name server.shop.dinhminhkhoa.id.vn name", profile)
        self.assertIn("script-security 1", profile)
        self.assertNotIn("persist-tun", profile)
        self.assertNotIn("resolv-retry infinite", profile)

    def test_wrong_exported_ca_is_rejected_before_starting_client(self):
        self.export = self.export.replace("YWJj", "ZGVm")
        with self.assertRaises(VpnFailure):
            with self.tunnel.opened(observations=1): self.fail("unowned trust chain")
        self.assertIsNone(self.process)

    def test_owned_secret_must_already_exist_and_keep_its_observed_version(self):
        ref = self.runtime.private["bootstrapSecrets"]["vpn-pki"]
        ref["phase"] = "CALLING"
        with self.assertRaises(ValueError): read_existing_secret(self.runtime, "vpn-pki", lambda: None)
        self.assertEqual(self.assets.secret_creates, 1)
        ref["phase"] = "OBSERVED"
        self.assets.secrets[ref["name"]]["VersionIdsToStages"] = {"different": ["AWSCURRENT"]}
        with self.assertRaises(ValueError): read_existing_secret(self.runtime, "vpn-pki", lambda: None)

    def test_lost_terminate_response_recovers_only_from_positive_terminated_status(self):
        self.lost_terminate = True
        with self.tunnel.opened(observations=1): pass
        self.assertTrue(self.tunnel.disconnected)
        self.assertEqual(len(self.terminations), 1)

    def test_empty_disconnect_read_is_not_positive_termination_proof(self):
        self.hide_terminate = True
        with self.assertRaises(VpnFailure):
            with self.tunnel.opened(observations=1): pass
        self.assertFalse(self.tunnel.disconnected)

    def test_fenced_session_still_stops_local_process_without_remote_mutation(self):
        with self.assertRaises(ValueError):
            with self.tunnel.opened(observations=1): self.guard_lost = True
        self.assertEqual(self.process.returncode, 0)
        self.assertFalse(self.terminations)
        self.assertFalse(self.profile_file.exists())

    def test_private_dns_must_resolve_shop_and_admitted_alb_to_same_owned_ip(self):
        with self.tunnel.opened(observations=1):
            self.tunnel.resolver.resolve = lambda name, *a, **k: ["10.52.10.5" if name.startswith("internal-") else "8.8.8.8"]
            with self.assertRaises(VpnFailure): self.tunnel.private_address()


if __name__ == "__main__": unittest.main()
