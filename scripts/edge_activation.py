"""Observe the committed edge, then expose only the session-owned shop CNAME.

ACTIVATED is a control-plane/DNS checkpoint. It never implies application,
public-network, authenticated management or end-to-end VPN acceptance.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import re
import time

from automation_budget import validate_estimate
from bootstrap_assets import read_cloudflare_client, save_private
from cleanup_schedule import verify_existing
from cloudflare_dns import HOST, owned_shop_record, validate_name_target, validate_shop_intent
from deployment_tasks import exposure_context, read_task, validate_activation, validate_exposure_checkpoint
from lifecycle_stage import StageController
from plan_review_store import checksum, immutable_json, read_json
from workload_state import read_state

ACCOUNT = "058114477594"
TAGS = dict(Project="techx", Environment="staging")


def read_activation_receipt(client, bucket, record, execution_version):
    stage = validate_exposure_checkpoint(record)
    ref = stage["receiptReference"]
    receipt, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    if (not isinstance(receipt, dict) or checksum(receipt) != ref["sha256"] or receipt.get("schemaVersion") != 1
            or receipt.get("sessionId") != record["sessionId"] or receipt.get("executionVersion") != execution_version
            or receipt.get("taskId") != stage["taskId"] or receipt.get("contextSha256") != stage["contextSha256"]
            or receipt.get("controlPlane") != "ready" or receipt.get("dns") != "converged" or receipt.get("acceptance") != "pending"
            or not re.fullmatch(r"[a-f0-9]{64}", receipt.get("stateSha256", ""))):
        raise ValueError("Activation receipt is missing, changed or belongs to another execution")
    return receipt


def resource(state, module, kind, name="this"):
    rows = [item for item in state["resources"] if item.get("mode") == "managed"
            and item.get("module") == module and item.get("type") == kind and item.get("name") == name]
    if len(rows) != 1 or len(rows[0].get("instances", [])) != 1 or rows[0]["instances"][0].get("deposed"):
        raise ValueError("Edge resource is missing, duplicated or being replaced in committed state")
    value = rows[0]["instances"][0].get("attributes")
    if not isinstance(value, dict): raise ValueError("Edge resource has no committed attributes")
    return value


def edge_binding(state, record, execution, private):
    edge = exposure_context(record)["edge"]
    if (state["lineage"] != edge["stateLineage"] or state["serial"] != edge["stateSerial"]
            or state["lineage"] != execution.get("initialState", {}).get("lineage")):
        raise ValueError("Edge state no longer matches the committed apply")
    distribution = resource(state, "module.cloudfront", "aws_cloudfront_distribution")
    origin = resource(state, "module.cloudfront", "aws_cloudfront_vpc_origin")
    vpn = resource(state, "module.client_vpn", "aws_ec2_client_vpn_endpoint")
    association = resource(state, "module.client_vpn", "aws_ec2_client_vpn_network_association")
    vpc = resource(state, "module.vpc", "aws_vpc")
    variables = private["terraformVars"]
    target = distribution.get("domain_name", "")
    validate_name_target(HOST, target)
    configured_origins = distribution.get("origin", [])
    if len(configured_origins) != 1 or configured_origins[0].get("origin_id") != "internal-alb":
        raise ValueError("Committed storefront must use its single internal ALB origin")
    binding = dict(distributionId=distribution.get("id", ""), distributionArn=distribution.get("arn", ""), target=target,
        originDomainName=configured_origins[0].get("domain_name", ""),
        originId=origin.get("id", ""), originArn=origin.get("arn", ""), albArn=record["stages"]["gitops"]["internalAlbArn"],
        endpointId=vpn.get("id", ""), associationId=association.get("id", ""), subnetId=association.get("subnet_id", ""),
        vpcId=vpc.get("id", ""), vpcCidr=vpc.get("cidr_block"), securityGroupIds=vpn.get("security_group_ids"),
        publicCertificateArn=variables["public_certificate_arn"], serverCertificateArn=variables["client_vpn_server_certificate_arn"],
        rootCertificateArn=variables["client_vpn_root_certificate_arn"])
    outputs = {key: value.get("value") for key, value in state.get("outputs", {}).items()}
    origin_config = origin.get("vpc_origin_endpoint_config", [])
    if (not re.fullmatch(r"[A-Z0-9]{8,32}", binding["distributionId"])
            or binding["distributionArn"] != f"arn:aws:cloudfront::{ACCOUNT}:distribution/{binding['distributionId']}"
            or not re.fullmatch(r"vo_[A-Za-z0-9_-]{8,64}", binding["originId"])
            or binding["originArn"] != f"arn:aws:cloudfront::{ACCOUNT}:vpcorigin/{binding['originId']}"
            or not re.fullmatch(r"cvpn-endpoint-[a-f0-9]{8,17}", binding["endpointId"])
            or not re.fullmatch(r"cvpn-assoc-[a-f0-9]{8,17}", binding["associationId"])
            or not re.fullmatch(r"subnet-[a-f0-9]{8,17}", binding["subnetId"])
            or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", binding["vpcId"]) or binding["vpcCidr"] != "10.52.0.0/16"
            or not isinstance(binding["securityGroupIds"], list) or len(binding["securityGroupIds"]) != 1
            or not re.fullmatch(r"sg-[a-f0-9]{8,17}", binding["securityGroupIds"][0])
            or association.get("client_vpn_endpoint_id") != binding["endpointId"] or vpn.get("vpc_id") != binding["vpcId"]
            or len(origin_config) != 1 or origin_config[0].get("arn") != binding["albArn"]
            or not re.fullmatch(r"internal-[A-Za-z0-9-]+\.us-east-1\.elb\.amazonaws\.com", binding["originDomainName"])
            or variables.get("internal_alb_arn") != binding["albArn"]
            or outputs.get("cloudfront_distribution_id") != binding["distributionId"] or outputs.get("cloudfront_domain_name") != target
            or outputs.get("client_vpn_endpoint_id") != binding["endpointId"]
            or not isinstance(outputs.get("private_subnet_ids"), list) or not outputs["private_subnet_ids"]
            or outputs["private_subnet_ids"][0] != binding["subnetId"]
            or outputs.get("public_frontend_url") != f"https://{HOST}/"):
        raise ValueError("Committed edge identities/outputs do not match the staging contract")
    return binding


class EdgeActivation:
    def __init__(self, runtime, store, version, task_id, *, clock, sleep=time.sleep, dns_factory=None, dns_verify=None):
        self.runtime, self.store, self.version, self.task_id = runtime, store, version, task_id
        self.clock, self.sleep = clock, sleep
        self.controller = StageController(runtime.clients["s3"], store, runtime.bucket, clock)
        self.dns_factory = dns_factory or (lambda: read_cloudflare_client(runtime, self.binding_guard))
        self.dns_verify = dns_verify or self.verify_dns

    def validate_record(self, record):
        if (any(record.get(key) != self.runtime.execution.get(key) for key in ("sessionId", "account", "region", "environment",
                "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"))
                or record.get("executionVersion") != self.version):
            raise ValueError("Activation differs from immutable execution")
        context = checksum(exposure_context(record))
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        if ((task["mode"], task["stage"], task["attempt"]) != ("activate", "exposure", 1)
                or record["deploymentTask"].get("state") != "RUNNING"
                or task["instanceId"] != self.runtime.private["deploymentExecutor"]["instanceId"]
                or task["expectedExposureSha256"] != context
                or hasattr(self, "context_hash") and context != self.context_hash):
            raise ValueError("Activation is not the current claimed task/predecessor")
        return record

    def guard(self):
        record, _ = self.controller.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.validate_record(record)
        validate_estimate(record, self.runtime.execution["costEstimate"])
        verify_existing(self.runtime.clients["scheduler"], record, self.version, self.clock())
        return record

    def binding_guard(self):
        record = self.guard()
        state = read_state(self.runtime.clients["s3"], self.runtime.bucket)
        if checksum(state) != self.state_hash or edge_binding(state, record, self.runtime.execution, self.runtime.private) != self.binding:
            raise ValueError("Committed edge state changed during activation")
        return record

    def call(self, service, method, **args):
        self.guard()
        return getattr(self.runtime.clients[service], method)(**args)

    def observe(self):
        b = self.binding
        distribution = self.call("cloudfront", "get_distribution", Id=b["distributionId"])["Distribution"]
        config = distribution.get("DistributionConfig", {})
        origins = config.get("Origins", {})
        rows = origins.get("Items", [])
        cert = config.get("ViewerCertificate", {})
        if (distribution.get("Id") != b["distributionId"] or distribution.get("ARN") != b["distributionArn"]
                or distribution.get("DomainName") != b["target"] or config.get("Enabled") is not True
                or config.get("Aliases") != dict(Quantity=1, Items=[HOST]) or origins.get("Quantity") != 1 or len(rows) != 1
                or rows[0].get("Id") != "internal-alb" or rows[0].get("VpcOriginConfig", {}).get("VpcOriginId") != b["originId"]
                or rows[0].get("DomainName") != b["originDomainName"]
                or config.get("DefaultCacheBehavior", {}).get("TargetOriginId") != "internal-alb"
                or config.get("DefaultCacheBehavior", {}).get("ViewerProtocolPolicy") != "redirect-to-https"
                or cert.get("ACMCertificateArn") != b["publicCertificateArn"] or cert.get("SSLSupportMethod") != "sni-only"
                or cert.get("MinimumProtocolVersion") != "TLSv1.2_2021"):
            raise ValueError("CloudFront observation differs from the committed storefront")
        tags = self.call("cloudfront", "list_tags_for_resource", Resource=b["distributionArn"])
        observed_tags = {row["Key"]: row["Value"] for row in tags.get("Tags", {}).get("Items", [])}
        if any(observed_tags.get(k) != v for k, v in TAGS.items()): raise ValueError("CloudFront ownership changed")
        origin = self.call("cloudfront", "get_vpc_origin", Id=b["originId"])["VpcOrigin"]
        if (origin.get("Id") != b["originId"] or origin.get("Arn") != b["originArn"] or origin.get("AccountId", ACCOUNT) != ACCOUNT
                or origin.get("VpcOriginEndpointConfig", {}).get("Arn") != b["albArn"]):
            raise ValueError("CloudFront origin differs from the bootstrapped internal ALB")
        result = self.call("ec2", "describe_client_vpn_endpoints", ClientVpnEndpointIds=[b["endpointId"]])
        endpoints = result.get("ClientVpnEndpoints", [])
        if result.get("NextToken") or len(endpoints) != 1: raise ValueError("VPN endpoint observation is ambiguous")
        vpn = endpoints[0]
        tags = {row["Key"]: row["Value"] for row in vpn.get("Tags", [])}
        if (vpn.get("ClientVpnEndpointId") != b["endpointId"] or vpn.get("VpcId") != b["vpcId"]
                or vpn.get("SecurityGroupIds") != b["securityGroupIds"] or vpn.get("ClientCidrBlock") != "172.24.0.0/22"
                or vpn.get("SplitTunnel") is not True or vpn.get("TransportProtocol") != "udp" or vpn.get("VpnPort") != 443
                or vpn.get("DnsServers") != ["10.52.0.2"] or vpn.get("ServerCertificateArn") != b["serverCertificateArn"]
                or vpn.get("AuthenticationOptions") != [dict(Type="certificate-authentication",
                    MutualAuthentication=dict(ClientRootCertificateChain=b["rootCertificateArn"]))]
                or any(tags.get(k) != v for k, v in TAGS.items())):
            raise ValueError("VPN configuration/ownership differs from staging mutual TLS")
        result = self.call("ec2", "describe_client_vpn_target_networks", ClientVpnEndpointId=b["endpointId"])
        networks = result.get("ClientVpnTargetNetworks", [])
        if result.get("NextToken") or len(networks) != 1: raise ValueError("VPN must have one observed association")
        network = networks[0]
        if (network.get("AssociationId") != b["associationId"] or network.get("ClientVpnEndpointId") != b["endpointId"]
                or network.get("TargetNetworkId") != b["subnetId"] or network.get("VpcId") != b["vpcId"]
                or network.get("SecurityGroups") != b["securityGroupIds"]):
            raise ValueError("VPN association differs from committed private subnet")
        statuses = (distribution.get("Status"), origin.get("Status"), vpn.get("Status", {}).get("Code"), network.get("Status", {}).get("Code"))
        if any(value not in allowed for value, allowed in zip(statuses,
                ({"Deployed", "InProgress"}, {"Deployed", "Deploying", "InProgress"}, {"available", "pending", "pending-associate"}, {"associated", "associating"}))):
            raise ValueError("Edge is failed, deleted or has an unknown readiness status")
        return statuses == ("Deployed", "Deployed", "available", "associated")

    def activate_dns(self):
        b, session = self.binding, self.runtime.execution["sessionId"]
        intended = dict(schemaVersion=1, phase="PREPARED", name=HOST, target=b["target"], distributionId=b["distributionId"])
        prior = self.runtime.private.get("shopDnsIntent")
        if prior is None:
            save_private(self.runtime, self.binding_guard, lambda value: value.update(shopDnsIntent=deepcopy(intended)))
        else:
            validate_shop_intent(prior)
            if any(prior[key] != intended[key] for key in intended if key != "phase"):
                raise ValueError("Storefront DNS intent changed")
            if prior["phase"] != "PREPARED" and self.runtime.private.get("dnsMutationAttempted") is not True:
                raise ValueError("Storefront mutation intent has no matching private mutation marker")
        client = self.dns_factory()
        self.binding_guard()
        records = client.records(HOST)
        prior = self.runtime.private["shopDnsIntent"]
        if prior["phase"] in ("CALLING", "OBSERVED"):
            # A lost POST response is not permission to create again.
            ownership = owned_shop_record(records, b["target"], session)
            if prior["phase"] == "OBSERVED" and ownership["id"] != prior["id"]:
                raise ValueError("Storefront record was replaced after observation")
        else:
            if records: owned_shop_record(records, b["target"], session)
            calling = dict(intended, phase="CALLING")
            save_private(self.runtime, self.binding_guard, lambda value: value.update(shopDnsIntent=calling, dnsMutationAttempted=True))
            self.binding_guard()
            response = client.ensure(HOST, b["target"], session)
            self.binding_guard()
            ownership = owned_shop_record(client.records(HOST), b["target"], session)
            if response != ownership: raise ValueError("DNS mutation response differs from the actual owned record")
        observed = dict(intended, phase="OBSERVED", id=ownership["id"])
        if self.runtime.private["shopDnsIntent"] != observed:
            save_private(self.runtime, self.binding_guard, lambda value: value.update(shopDnsIntent=observed, dnsMutationAttempted=True))
        return ownership

    @staticmethod
    def verify_dns(target, guard):
        spec = importlib.util.spec_from_file_location("edge_public_dns", Path(__file__).with_name("public-dns.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.verify_target(target, guard)

    def perform(self, owner, epoch, *, observations=120):
        if type(observations) is not int or not 1 <= observations <= 120: raise ValueError("Bounded edge observations are required")
        self.owner, self.epoch = owner, epoch
        record = self.guard()
        self.context_hash = validate_activation(record, 1)
        self.runtime.executor()
        state = read_state(self.runtime.clients["s3"], self.runtime.bucket)
        self.binding = edge_binding(state, record, self.runtime.execution, self.runtime.private)
        self.state_hash = checksum(state)
        for _ in range(observations):
            self.binding_guard()
            if self.observe(): break
            self.sleep(10)
        else: raise TimeoutError("CloudFront/VPN readiness was not proven")
        ownership = self.activate_dns()
        for _ in range(observations):
            self.binding_guard()
            try:
                self.dns_verify(self.binding["target"], self.binding_guard)
                break
            except (ValueError, OSError):
                self.binding_guard()  # A fenced/changed session must not wait/retry.
                self.sleep(10)
        else: raise TimeoutError("Recursive and authoritative DNS did not converge")
        self.binding_guard()
        receipt = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=self.version, taskId=self.task_id,
            contextSha256=self.context_hash, stateSha256=self.state_hash, resources=self.binding, ownership=ownership,
            controlPlane="ready", dns="converged", acceptance="pending")
        key = f"private/staging/{record['sessionId']}/exposure/{self.task_id}.json"
        reference = immutable_json(self.runtime.clients["s3"], self.runtime.bucket, key, receipt)
        persisted, _ = read_json(self.runtime.clients["s3"], self.runtime.bucket, key, reference["versionId"])
        if persisted != receipt: raise ValueError("Persisted activation receipt differs")
        reference["sha256"] = checksum(receipt)
        self.binding_guard()
        current, etag = self.controller.owned(record["sessionId"], self.owner, self.epoch)
        self.validate_record(current)
        if current.get("stages", {}).get("exposure"): raise ValueError("Activation checkpoint was already consumed")
        updated = deepcopy(current)
        updated["stages"]["exposure"] = dict(state="ACTIVATED", attempt=1, taskId=self.task_id,
            contextSha256=self.context_hash, receiptReference=reference, completedAt=self.clock().isoformat())
        self.store.write(updated, etag)
        return dict(state="ACTIVATED", acceptance="pending", receiptReference=reference)
