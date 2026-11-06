"""Mock Terraform network -> EC2 SDK response -> actual executor factory.

The Terraform test suite uses mock_provider aws. No infrastructure is created;
the test deliberately consumes declared rules rather than duplicating their
expected list in another disconnected SDK fixture.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest

import botocore.session
from botocore.validate import validate_parameters

spec = importlib.util.spec_from_file_location("executor_network_fixture", Path(__file__).with_name("test-deployment-executor.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class NetworkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        directory = root / "bootstrap/automation"
        data = root / ".terraform-offline/bootstrap/automation"
        if not data.exists(): raise AssertionError("Initialize offline Terraform through scripts/verify.ps1 first")
        # Test state is ephemeral and separate from an operator's local state.
        state_file = directory / "terraform.tfstate"
        before = state_file.read_bytes() if state_file.exists() else None
        result = subprocess.run(["terraform", "test", "-json", "-verbose"], cwd=directory,
            env=dict(os.environ, TF_DATA_DIR=str(data)), capture_output=True, timeout=120)
        after = state_file.read_bytes() if state_file.exists() else None
        if before != after: raise AssertionError("Offline Terraform tests changed operator state")
        if result.returncode: raise AssertionError("Terraform mock network contract failed: " + result.stderr.decode(errors="replace")[-600:])
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        states = [event["test_state"] for event in events if event.get("type") == "test_state"
            and event.get("@testrun") == "lifecycle_is_scoped_and_pinned"]
        if len(states) != 1: raise AssertionError("The exact lifecycle mock state must be observed, not zero filtered tests")
        resources = states[0]["root_module"]["resources"]
        groups = [row["values"] for row in resources if row["address"] == "aws_security_group.executor_control[0]"]
        if len(groups) != 1 or groups[0].get("ingress") or groups[0].get("egress"):
            raise AssertionError("Executor SG must use explicit separate rules without inline ingress/egress")
        group_id = groups[0]["id"]
        ingress = [row for row in resources if row["type"] == "aws_vpc_security_group_ingress_rule"
            and row["values"].get("security_group_id") == group_id]
        if ingress: raise AssertionError("Executor SG cannot acquire inbound rules")
        cls.permissions = [dict(IpProtocol=row["values"]["ip_protocol"], FromPort=row["values"]["from_port"],
            ToPort=row["values"]["to_port"], IpRanges=[dict(CidrIp=row["values"]["cidr_ipv4"])])
            for row in resources if row["type"] == "aws_vpc_security_group_egress_rule"
            and row["values"].get("security_group_id") == group_id]
        if not cls.permissions: raise AssertionError("No declared executor egress was observed")

    def fixture(self, permissions):
        fixture = support.ExecutorTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.group["IpPermissionsEgress"] = deepcopy(permissions)
        shape = botocore.session.get_session().get_service_model("ec2").operation_model("DescribeSecurityGroups")
        def groups(**kwargs):
            validate_parameters(kwargs, shape.input_shape)
            self.assertEqual(kwargs["GroupIds"], [fixture.config["securityGroupId"]])
            response = dict(SecurityGroups=[deepcopy(fixture.group)])
            validate_parameters(response, shape.output_shape)
            return response
        fixture.ec2.describe_security_groups = groups
        return fixture

    def test_the_declared_terraform_network_passes_the_actual_factory_before_launch(self):
        fixture = self.fixture(self.permissions)
        result = fixture.factory.prepare(observations=1)
        self.assertEqual(result["deploymentExecutor"]["instanceId"], fixture.f.instance_id)
        self.assertEqual(len(fixture.launched), 1)

    def test_missing_vpn_or_broader_udp_cannot_launch_an_executor(self):
        without_vpn = [row for row in self.permissions if (row["IpProtocol"], row["FromPort"], row["ToPort"]) != ("udp", 443, 443)]
        self.assertNotEqual(without_vpn, self.permissions)
        broad = deepcopy(self.permissions)
        vpn = next(row for row in broad if row["IpProtocol"] == "udp" and row["FromPort"] == 443)
        vpn.update(FromPort=0, ToPort=65535)
        for permissions in (without_vpn, broad):
            fixture = self.fixture(permissions)
            with self.subTest(permissions=permissions), self.assertRaises(ValueError): fixture.factory.prepare(observations=1)
            self.assertFalse(fixture.launched)


if __name__ == "__main__": unittest.main()
