from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from deployment_executor import ExecutorFactory, validate_launch
from deployment_manager import PROFILE

spec = importlib.util.spec_from_file_location("executor_manager_support", Path(__file__).with_name("test-deployment-manager.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.f = support.ManagerTests()
        self.f.setUp()
        self.f.private.pop("deploymentExecutor")
        self.config = self.f.config
        tags = [dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="automation", Role="deployment-control").items()]
        self.image = dict(ImageId=self.config["imageId"], OwnerId="058114477594", State="available", Architecture="x86_64",
            PlatformDetails="Linux/UNIX", RootDeviceType="ebs", RootDeviceName="/dev/sda1",
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Role="deployment-executor-image", LoaderSha256=self.config["loaderSha256"], BuildSha256=self.config["imageBuildSha256"]).items()])
        self.template = dict(ImageId=self.config["imageId"], InstanceType="t3.small", IamInstanceProfile=dict(Arn=PROFILE),
            MetadataOptions=dict(HttpEndpoint="enabled", HttpTokens="required", HttpPutResponseHopLimit=1, InstanceMetadataTags="disabled"),
            NetworkInterfaces=[dict(DeviceIndex=0, AssociatePublicIpAddress=True, DeleteOnTermination=True,
                                    SubnetId=self.config["subnetId"], Groups=[self.config["securityGroupId"]])],
            BlockDeviceMappings=[dict(DeviceName="/dev/sda1", Ebs=dict(VolumeSize=20, VolumeType="gp3", Encrypted=True, DeleteOnTermination=True))],
            CreditSpecification=dict(CpuCredits="standard"), InstanceInitiatedShutdownBehavior="terminate", DisableApiTermination=False, DisableApiStop=True)
        self.subnet = dict(SubnetId=self.config["subnetId"], OwnerId="058114477594", VpcId="vpc-12345678", Tags=deepcopy(tags))
        self.vpc = dict(VpcId="vpc-12345678", CidrBlock="172.30.0.0/24", IsDefault=False, State="available", Tags=deepcopy(tags))
        self.group = dict(GroupId=self.config["securityGroupId"], OwnerId="058114477594", VpcId="vpc-12345678", IpPermissions=[], Tags=deepcopy(tags),
            IpPermissionsEgress=[dict(IpProtocol=p, FromPort=port, ToPort=port, IpRanges=[dict(CidrIp=cidr)]) for p, port, cidr in
                                 (("tcp", 443, "0.0.0.0/0"), ("tcp", 53, "172.30.0.2/32"), ("udp", 53, "172.30.0.2/32"))])
        self.route = dict(VpcId="vpc-12345678", Routes=[dict(DestinationCidrBlock="0.0.0.0/0", GatewayId="igw-12345678", State="active")])
        self.ec2 = self.f.clients["ec2"]
        self.ec2.describe_images = lambda **kw: dict(Images=[deepcopy(self.image)])
        self.ec2.describe_launch_template_versions = lambda **kw: dict(LaunchTemplateVersions=[dict(
            LaunchTemplateId=self.config["launchTemplateId"], VersionNumber=1, LaunchTemplateData=deepcopy(self.template))])
        self.ec2.describe_subnets = lambda **kw: dict(Subnets=[deepcopy(self.subnet)])
        self.ec2.describe_vpcs = lambda **kw: dict(Vpcs=[deepcopy(self.vpc)])
        self.ec2.describe_security_groups = lambda **kw: dict(SecurityGroups=[deepcopy(self.group)])
        self.ec2.describe_route_tables = lambda **kw: dict(RouteTables=[deepcopy(self.route)])
        self.launched, self.private_etag, self.online = [], 1, True
        self.run_callback = None
        model = botocore.session.get_session().get_service_model("ec2")
        def run_instances(**kw):
            validate_parameters(kw, model.operation_model("RunInstances").input_shape)
            self.assertEqual(self.f.record["lease"]["owner"], self.f.owner)
            self.assertIn("deploymentLaunch", self.f.private)
            self.assertEqual(self.f.private["deploymentLaunch"]["phase"], "CALLING")
            self.assertEqual(kw["MinCount"], kw["MaxCount"], 1)
            self.launched.append(kw)
            if self.run_callback: self.run_callback()
            return dict(OwnerId="058114477594", Instances=[deepcopy(self.f.instance)])
        self.ec2.run_instances = run_instances
        self.f.clients["ssm"].describe_instance_information = lambda **kw: dict(InstanceInformationList=[dict(
            InstanceId=self.f.instance_id, PlatformType="Linux", PingStatus="Online" if self.online else "ConnectionLost", AgentVersion="3.3.3000.0")])
        def get_object(**kw):
            return dict(Body=io.BytesIO(json.dumps(self.f.private).encode()), ETag=str(self.private_etag))
        def put_object(**kw):
            self.assertEqual(kw["ExpectedBucketOwner"], "058114477594")
            self.assertEqual(kw["IfMatch"], str(self.private_etag))
            self.f.private = json.loads(kw["Body"])
            self.private_etag += 1
        self.f.clients["s3"].get_object = get_object
        self.f.clients["s3"].put_object = put_object
        self.factory = ExecutorFactory(self.f.manager)

    def test_prepares_one_owned_vm_and_reuses_it_without_launching_a_replacement(self):
        private = self.factory.prepare(observations=1)
        self.assertEqual(private["deploymentExecutor"]["instanceId"], self.f.instance_id)
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["executorPrepared"], dict(instanceId=self.f.instance_id, imageId=self.config["imageId"], managerArn=self.f.owner))
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.factory.prepare(observations=1)["deploymentExecutor"], private["deploymentExecutor"])
        self.assertEqual(len(self.launched), 1)
        self.assertFalse(self.f.cleanup)
        self.assertEqual({row["ResourceType"] for row in self.launched[0]["TagSpecifications"]}, {"instance", "volume"})

    def test_ami_template_and_network_guards_prevent_paid_launch(self):
        for obj, key, value in ((self.image, "OwnerId", "123456789012"), (self.image, "Architecture", "arm64"),
                (self.template, "InstanceType", "m5.24xlarge"), (self.template, "UserData", "arbitrary-command"),
                (self.vpc, "IsDefault", True), (self.group, "IpPermissions", [dict(IpProtocol="-1")])):
            present, old = key in obj, deepcopy(obj.get(key))
            obj[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.factory.prepare(observations=1)
            if present: obj[key] = old
            else: del obj[key]
            self.assertFalse(self.launched)

    def test_nat_route_or_broad_egress_is_rejected(self):
        self.route["Routes"][0]["GatewayId"] = "nat-12345678"
        with self.assertRaises(ValueError): validate_launch(self.ec2, self.config)
        self.route["Routes"][0]["GatewayId"] = "igw-12345678"
        self.group["IpPermissionsEgress"][0]["IpProtocol"] = "-1"
        with self.assertRaises(ValueError): validate_launch(self.ec2, self.config)

    def test_unknown_launch_response_keeps_intent_and_requests_cleanup_no_second_run(self):
        def lost(): raise TimeoutError("RunInstances may have succeeded")
        self.run_callback = lost
        with self.assertRaises(TimeoutError): self.factory.prepare(observations=1)
        self.assertIn("deploymentLaunch", self.f.private)
        self.assertNotIn("deploymentExecutor", self.f.private)
        self.assertNotIn("lease", self.f.record)
        self.assertTrue(self.f.cleanup)
        with self.assertRaises(ValueError): self.factory.prepare(observations=1)
        self.assertEqual(len(self.launched), 1)

    def test_interruption_after_prepared_intent_never_invokes_ec2(self):
        original_guard = self.factory.guard
        def interrupted(epoch):
            if self.f.private.get("deploymentLaunch", {}).get("phase") == "PREPARED":
                raise TimeoutError("Manager stopped before durable CALLING checkpoint")
            return original_guard(epoch)
        self.factory.guard = interrupted
        with self.assertRaises(TimeoutError): self.factory.prepare(observations=1)
        self.assertEqual(self.f.private["deploymentLaunch"]["phase"], "PREPARED")
        self.assertFalse(self.launched)
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("lease", self.f.record)

    def test_ssm_timeout_keeps_executor_reference_for_independent_cleanup(self):
        self.online = False
        with self.assertRaises(TimeoutError): self.factory.prepare(observations=2)
        self.assertEqual(self.f.private["deploymentExecutor"]["instanceId"], self.f.instance_id)
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("lease", self.f.record)

    def test_cleanup_takeover_after_launch_cannot_be_overwritten(self):
        def takeover():
            self.f.record["status"] = "DESTROYING"
            self.f.record["lease"] = dict(owner="cleanup", operation="cleanup", epoch=99)
        self.run_callback = takeover
        with self.assertRaises(ValueError): self.factory.prepare(observations=1)
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["lease"]["owner"], "cleanup")
        self.assertIn("deploymentLaunch", self.f.private)


if __name__ == "__main__": unittest.main()
