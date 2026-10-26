"""Actual activation orchestration with SDK-shaped observations and durable stores."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim, release
from cloudflare_dns import CloudflareDNS, HOST
from deployment_tasks import claim_task, create_task, finish_task, read_task, validate_exposure_checkpoint
from edge_activation import EdgeActivation, read_activation_receipt
from plan_review_store import checksum
from shop_dns_recovery import cleanup_shop_dns

spec = importlib.util.spec_from_file_location("activation_manager_fixture", Path(__file__).with_name("test-deployment-manager.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)

LINEAGE = "12345678-1234-1234-1234-123456789abc"
ACCOUNT = "058114477594"


def applied_edge(record):
    ref = dict(key=f"chart-selections/staging/{record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="d" * 64)
    release_ref = dict(key="release", versionId="release-v1", sha256="e" * 64)
    record.update(status="PROVISIONING", bootstrapChart=ref, bootstrapRelease=release_ref)
    record["stages"] = dict(foundation=dict(state="APPLIED", attempt=1), edge=dict(state="APPLIED", attempt=1,
        stateSerial=3, stateLineage=LINEAGE), gitops=dict(state="BOOTSTRAPPED", attempt=1, chartSha="b" * 40,
        selectionReference=deepcopy(ref), releaseReference=deepcopy(release_ref),
        internalAlbArn=f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/techx/123abc"))


class ActivationTests(unittest.TestCase):
    def setUp(self):
        f = self.f = support.ManagerTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        applied_edge(f.record)
        f.execution.update(initialState=dict(lineage=LINEAGE, serial=0), costEstimate=dict(
            approvedBudgetUsd=60, maximumHours=4, upperBoundUsd=20, includesAutomation=True))
        f.private.update(dnsMutationAttempted=False, validationDnsPlans=[])
        self.alb = f.record["stages"]["gitops"]["internalAlbArn"]
        self.cert = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/{LINEAGE}"
        f.private["terraformVars"].update(internal_alb_arn=self.alb, public_certificate_arn=self.cert,
            client_vpn_server_certificate_arn=self.cert, client_vpn_root_certificate_arn=self.cert)
        self.state = dict(version=4, lineage=LINEAGE, serial=3, resources=[], outputs={})
        def resource(module, kind, **attributes):
            self.state["resources"].append(dict(mode="managed", module=module, type=kind, name="this", instances=[dict(attributes=attributes)]))
        self.distribution_id = "E123ABC456DEF"
        self.distribution_arn = f"arn:aws:cloudfront::{ACCOUNT}:distribution/{self.distribution_id}"
        self.origin_id = "vo_12345678abcd"
        self.origin_arn = f"arn:aws:cloudfront::{ACCOUNT}:vpcorigin/{self.origin_id}"
        self.target = "d123abc.cloudfront.net"
        self.vpn_id, self.assoc = "cvpn-endpoint-1234567890abcdef0", "cvpn-assoc-1234567890abcdef0"
        self.vpc, self.subnet, self.sg = "vpc-1234567890abcdef0", "subnet-1234567890abcdef0", "sg-1234567890abcdef0"
        self.origin_domain = "internal-techx.us-east-1.elb.amazonaws.com"
        resource("module.cloudfront", "aws_cloudfront_distribution", id=self.distribution_id, arn=self.distribution_arn,
            domain_name=self.target, origin=[dict(origin_id="internal-alb", domain_name=self.origin_domain)])
        resource("module.cloudfront", "aws_cloudfront_vpc_origin", id=self.origin_id, arn=self.origin_arn,
            vpc_origin_endpoint_config=[dict(arn=self.alb)])
        resource("module.client_vpn", "aws_ec2_client_vpn_endpoint", id=self.vpn_id, vpc_id=self.vpc, security_group_ids=[self.sg])
        resource("module.client_vpn", "aws_ec2_client_vpn_network_association", id=self.assoc, client_vpn_endpoint_id=self.vpn_id, subnet_id=self.subnet)
        resource("module.vpc", "aws_vpc", id=self.vpc, cidr_block="10.52.0.0/16")
        self.state["outputs"] = {key: dict(value=value) for key, value in dict(cloudfront_distribution_id=self.distribution_id,
            cloudfront_domain_name=self.target, client_vpn_endpoint_id=self.vpn_id, private_subnet_ids=[self.subnet],
            public_frontend_url=f"https://{HOST}/", order_table_name="techx-staging-orders",
            order_table_arn=f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/techx-staging-orders",
            order_api_role_arn=f"arn:aws:iam::{ACCOUNT}:role/techx-staging-order-api").items()}
        self.events, self.dns_records, self.mutations = [], [], []
        self.lose_post = self.lose_delete = self.hide_after_post = False
        self.before_receipt = None
        self.private_etag = 1
        original_get, original_put = f.clients["s3"].get_object, f.clients["s3"].put_object
        def get(**kw):
            if kw["Key"] == "staging/terraform.tfstate": return dict(Body=io.BytesIO(json.dumps(self.state).encode()))
            if kw["Key"].endswith("inputs.json"): return dict(ETag=str(self.private_etag), Body=io.BytesIO(json.dumps(f.private).encode()))
            if kw["Key"] not in f.objects: raise f.Missing()
            return original_get(**kw)
        def put(**kw):
            if kw["Key"].endswith("inputs.json"):
                self.assertEqual(kw["IfMatch"], str(self.private_etag))
                f.private = json.loads(kw["Body"])
                self.private_etag += 1
                return dict(VersionId="private-v" + str(self.private_etag))
            if "/exposure/" in kw["Key"] and self.before_receipt: self.before_receipt()
            return original_put(**kw)
        f.clients["s3"].get_object, f.clients["s3"].put_object = get, put
        self.task_id = "9" * 32
        self.task, pointer = create_task(f.clients["s3"], "control-bucket", f.record, "execution-v1", f.instance_id,
            f.owner, "activate", "exposure", 1, self.task_id, f.now)
        f.record["deploymentTask"] = pointer
        claim_task(f.store, f.clients["s3"], "control-bucket", self.task_id, f.instance_id, f.now)
        self.owner = f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{f.instance_id}"
        f.record = claim(f.record, self.owner, "deploy", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.distribution = dict(Id=self.distribution_id, ARN=self.distribution_arn, DomainName=self.target, Status="Deployed",
            DistributionConfig=dict(Enabled=True, Aliases=dict(Quantity=1, Items=[HOST]), Origins=dict(Quantity=1, Items=[
                dict(Id="internal-alb", DomainName=self.origin_domain, VpcOriginConfig=dict(VpcOriginId=self.origin_id))]),
                DefaultCacheBehavior=dict(TargetOriginId="internal-alb", ViewerProtocolPolicy="redirect-to-https"),
                ViewerCertificate=dict(ACMCertificateArn=self.cert, SSLSupportMethod="sni-only", MinimumProtocolVersion="TLSv1.2_2021")))
        self.origin = dict(Id=self.origin_id, Arn=self.origin_arn, Status="Deployed", VpcOriginEndpointConfig=dict(Arn=self.alb))
        self.vpn = dict(ClientVpnEndpointId=self.vpn_id, VpcId=self.vpc, SecurityGroupIds=[self.sg], ClientCidrBlock="172.24.0.0/22",
            SplitTunnel=True, TransportProtocol="udp", VpnPort=443, DnsServers=["10.52.0.2"], Status=dict(Code="available"),
            ServerCertificateArn=self.cert, AuthenticationOptions=[dict(Type="certificate-authentication", MutualAuthentication=dict(ClientRootCertificateChain=self.cert))],
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="staging").items()])
        self.network = dict(AssociationId=self.assoc, ClientVpnEndpointId=self.vpn_id, VpcId=self.vpc,
            TargetNetworkId=self.subnet, SecurityGroups=[self.sg], Status=dict(Code="associated"))
        self.network_response = dict(ClientVpnTargetNetworks=[self.network])
        sdk = botocore.session.get_session()
        def api(service, operation, result):
            def read(**kw):
                model = sdk.get_service_model(service).operation_model(operation)
                validate_parameters(kw, model.input_shape)
                self.events.append(operation)
                return deepcopy(result())
            return read
        f.clients["cloudfront"] = SimpleNamespace(
            get_distribution=api("cloudfront", "GetDistribution", lambda: dict(Distribution=self.distribution)),
            list_tags_for_resource=api("cloudfront", "ListTagsForResource", lambda: dict(Tags=dict(Items=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="staging").items()]))),
            get_vpc_origin=api("cloudfront", "GetVpcOrigin", lambda: dict(VpcOrigin=self.origin)))
        f.clients["ec2"].describe_client_vpn_endpoints = api("ec2", "DescribeClientVpnEndpoints", lambda: dict(ClientVpnEndpoints=[self.vpn]))
        f.clients["ec2"].describe_client_vpn_target_networks = api("ec2", "DescribeClientVpnTargetNetworks", lambda: self.network_response)
        self.runtime = SimpleNamespace(execution=f.execution, private=deepcopy(f.private), clients=f.clients, bucket="control-bucket",
            kms="test-kms", executor=lambda: self.events.append("executor-identity"))
        def dns_request(method, path, payload=None):
            if method == "GET": return dict(result=deepcopy(self.dns_records))
            self.events.append(method)
            if method == "POST":
                self.assertEqual(f.private["shopDnsIntent"]["phase"], "CALLING")
                self.assertTrue(f.private["dnsMutationAttempted"])
                self.mutations.append(payload)
                self.dns_records = [dict(payload, id="a" * 32)]
                if self.hide_after_post: self.dns_records = []
                if self.lose_post: raise TimeoutError("POST response lost")
                return dict(result=dict(payload, id="a" * 32))
            if method == "DELETE":
                self.assertIn(f"cleanup/{f.record['sessionId']}/shop-dns-observed.json", f.objects)
                self.dns_records = []
                if self.lose_delete: raise TimeoutError("DELETE response lost")
                return dict(result={})
            raise AssertionError(method)
        self.dns_request = dns_request
        self.worker = EdgeActivation(self.runtime, f.store, "execution-v1", self.task_id, clock=lambda: f.now,
            sleep=lambda _: self.events.append("sleep"), dns_verify=self.verify_dns)
        self.worker.dns_factory = lambda: CloudflareDNS("1" * 32, "test-only", self.dns_request, guard=self.worker.binding_guard)
        self.cleanup_client = CloudflareDNS("1" * 32, "test-only", self.dns_request)

    def verify_dns(self, target, guard):
        guard()
        self.assertEqual(target, self.target)
        self.events.append("positive-dns")

    def perform(self, observations=1): return self.worker.perform(self.owner, self.epoch, observations=observations)

    def cleanup_dns(self):
        cleanup_shop_dns(self.f.clients["s3"], "control-bucket", self.f.record["sessionId"], self.f.private, self.cleanup_client)

    def test_committed_edge_observations_and_dns_receipt_do_not_mark_ready(self):
        result = self.perform()
        self.assertEqual(result["acceptance"], "pending")
        self.assertEqual(self.f.record["status"], "PROVISIONING")
        self.assertEqual(self.f.private["shopDnsIntent"]["phase"], "OBSERVED")
        checkpoint = validate_exposure_checkpoint(self.f.record, self.task)
        payload = json.loads(self.f.objects[checkpoint["receiptReference"]["key"]][0])
        self.assertEqual(payload["stateSha256"], checksum(self.state))
        self.assertEqual(payload["resources"]["associationId"], self.assoc)
        self.assertEqual(read_activation_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1"), payload)
        with self.assertRaises(ValueError):
            read_activation_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "another-version")
        self.assertLess(self.events.index("DescribeClientVpnTargetNetworks"), self.events.index("POST"))
        self.assertLess(self.events.index("POST"), self.events.index("positive-dns"))
        self.f.record = release(self.f.record, self.owner, self.epoch)
        finish_task(self.f.store, self.task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")

    def test_in_progress_never_creates_dns_and_wait_is_bounded(self):
        self.distribution["Status"] = "InProgress"
        with self.assertRaises(TimeoutError): self.perform(2)
        self.assertEqual(self.events.count("GetDistribution"), 2)
        self.assertFalse(self.mutations)
        self.assertNotIn("shopDnsIntent", self.f.private)

    def test_wrong_vpn_subnet_or_extra_association_is_rejected_before_dns(self):
        self.network["TargetNetworkId"] = "subnet-00000000"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.mutations)
        self.network["TargetNetworkId"] = self.subnet
        self.network_response["NextToken"] = "more"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.mutations)

    def test_changed_state_and_predecessors_are_rejected(self):
        self.state["serial"] += 1
        with self.assertRaises(ValueError): self.perform()
        self.state["serial"] -= 1
        self.f.record["stages"]["gitops"]["chartSha"] = "f" * 40
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], "control-bucket", self.f.record, self.task_id, self.f.now)
        self.assertFalse(self.mutations)

    def test_origin_certificate_and_disabled_distribution_are_not_ready(self):
        for change in (lambda: self.origin["VpcOriginEndpointConfig"].update(Arn=self.alb + "0"),
                lambda: self.distribution["DistributionConfig"].update(Enabled=False),
                lambda: self.distribution["DistributionConfig"]["ViewerCertificate"].update(ACMCertificateArn="foreign")):
            original_origin, original_distribution = deepcopy(self.origin), deepcopy(self.distribution)
            change()
            with self.assertRaises(ValueError): self.perform()
            self.origin, self.distribution = original_origin, original_distribution
        self.assertFalse(self.mutations)

    def test_foreign_or_changed_owned_target_is_never_replaced(self):
        for comment in ("another owner", f"techx:{self.f.record['sessionId']}"):
            self.dns_records = [dict(id="a" * 32, type="CNAME", name=HOST, content="dother.cloudfront.net", proxied=False, comment=comment)]
            with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.mutations)

    def test_lost_create_response_reconciles_actual_record_without_duplicate_post(self):
        self.lose_post = True
        with self.assertRaises(TimeoutError): self.perform()
        self.assertEqual(self.f.private["shopDnsIntent"]["phase"], "CALLING")
        self.lose_post = False
        self.perform()
        self.assertEqual(len(self.mutations), 1)

    def test_unknown_create_with_empty_observation_never_retries_or_completes_cleanup(self):
        self.lose_post = self.hide_after_post = True
        with self.assertRaises(TimeoutError): self.perform()
        with self.assertRaises(ValueError): self.perform()
        with self.assertRaisesRegex(ValueError, "outcome remains unproven"): self.cleanup_dns()
        self.assertEqual(len(self.mutations), 1)
        self.assertNotIn("exposure", self.f.record["stages"])

    def test_cleanup_persists_creation_proof_before_lost_delete_and_can_resume_absent(self):
        self.lose_post = True
        with self.assertRaises(TimeoutError): self.perform()
        self.lose_delete = True
        with self.assertRaises(TimeoutError): self.cleanup_dns()
        self.lose_delete = False
        self.cleanup_dns()
        self.assertFalse(self.dns_records)
        self.assertEqual(self.events.count("DELETE"), 1)

    def test_cleanup_refuses_changed_target_even_with_own_session_comment(self):
        self.perform()
        self.dns_records[0]["content"] = "dother.cloudfront.net"
        with self.assertRaises(ValueError): self.cleanup_dns()
        self.assertNotIn("DELETE", self.events)

    def test_lease_expiry_or_cleanup_takeover_blocks_post_and_receipt(self):
        def expire(target, guard):
            self.f.record["lease"]["expiresAt"] = (self.f.now - timedelta(seconds=1)).isoformat()
            guard()
        self.worker.dns_verify = expire
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("exposure", self.f.record["stages"])
        self.assertFalse(any("/exposure/" in key for key in self.f.objects))

    def test_dns_outage_never_marks_activated(self):
        self.worker.dns_verify = lambda *args: (_ for _ in ()).throw(TimeoutError("DNS unavailable"))
        with self.assertRaises(TimeoutError): self.perform(2)
        self.assertNotIn("exposure", self.f.record["stages"])
        self.assertEqual(self.events.count("POST"), 1)
        self.cleanup_dns()

    def test_state_or_cleanup_change_after_receipt_cannot_publish_checkpoint(self):
        self.before_receipt = lambda: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotIn("exposure", self.f.record["stages"])

    def test_cleanup_cannot_delete_until_positive_observation_is_persisted(self):
        self.lose_post = True
        with self.assertRaises(TimeoutError): self.perform()
        original = self.f.clients["s3"].put_object
        def fail_write(**kw):
            if kw["Key"].endswith("shop-dns-observed.json"): raise TimeoutError("proof write unavailable")
            return original(**kw)
        self.f.clients["s3"].put_object = fail_write
        with self.assertRaises(TimeoutError): self.cleanup_dns()
        self.assertNotIn("DELETE", self.events)
        self.f.clients["s3"].put_object = original
        self.cleanup_dns()
        self.assertFalse(self.dns_records)

    def test_state_changes_during_edge_observation_block_dns_post(self):
        original = self.f.clients["ec2"].describe_client_vpn_target_networks
        def observe(**kw):
            result = original(**kw)
            self.state["serial"] += 1
            return result
        self.f.clients["ec2"].describe_client_vpn_target_networks = observe
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.mutations)

    def test_wrong_cleanup_proof_cannot_authorize_absent_calling_record(self):
        self.lose_post = self.hide_after_post = True
        with self.assertRaises(TimeoutError): self.perform()
        self.f.objects[f"cleanup/{self.f.record['sessionId']}/shop-dns-observed.json"] = json.dumps(dict(
            schemaVersion=1, sessionId="other-session", intentSha256="a" * 64, ownership={})).encode(), "proof-v1"
        with self.assertRaises(ValueError): self.cleanup_dns()

    def test_inconsistent_private_mutation_marker_cannot_resume_calling(self):
        self.lose_post = True
        with self.assertRaises(TimeoutError): self.perform()
        self.runtime.private["dnsMutationAttempted"] = self.f.private["dnsMutationAttempted"] = False
        with self.assertRaisesRegex(ValueError, "mutation marker"): self.perform()
        with self.assertRaisesRegex(ValueError, "inconsistent"): self.cleanup_dns()
        self.assertEqual(len(self.mutations), 1)

    def test_changed_receipt_body_is_rejected_even_with_original_version_reference(self):
        self.perform()
        ref = self.f.record["stages"]["exposure"]["receiptReference"]
        data, version = self.f.objects[ref["key"]]
        changed = json.loads(data)
        changed["dns"] = "unproven"
        self.f.objects[ref["key"]] = json.dumps(changed).encode(), version
        with self.assertRaises(ValueError):
            read_activation_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")


if __name__ == "__main__": unittest.main()
