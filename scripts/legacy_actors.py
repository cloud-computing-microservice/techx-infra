"""Collect legacy EC2/SSM actor observations through authenticated read-only SDK.

Discovery is intentionally wider than a session tag. Original role profiles,
shared TechX staging instances and automation instances without v2 identity are
included. Current absence does not establish historical terminal/drain proof.
"""
from copy import deepcopy
from datetime import timedelta
import argparse
import json
from pathlib import Path
import re

from execution_v2 import ACCOUNT, REGION
from legacy_authority import AuthorityCollector, digest
from legacy_reconciliation import ENDPOINTS, IncompleteAudit, LegacyCollector, PrivateEvidence, ReadOnlySDK, wire
from recovery_v2 import service_time

INSTANCE = re.compile(r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})")
PROFILE = re.compile(rf"arn:aws:iam::{ACCOUNT}:instance-profile/[A-Za-z0-9+=,.@_/-]+")
COMMAND = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
STATES = {"pending", "running", "stopping", "stopped", "shutting-down", "terminated"}
SSM_STATES = {"Pending", "InProgress", "Delayed", "Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
SSM_TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed"}
_SEAL = object()


class ActorObservations:
    __slots__ = ("report", "evidence_path")

    def __init__(self, report, path, *, _seal=None):
        if _seal is not _SEAL: raise ValueError("Use authenticated actor collection")
        self.report, self.evidence_path = deepcopy(report), str(path)


def tags(value):
    if value is None: return {}
    if not isinstance(value, list): raise IncompleteAudit("INVALID_INSTANCE_TAGS")
    found = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("Key"), str) or not isinstance(row.get("Value"), str) or row["Key"] in found:
            raise IncompleteAudit("INVALID_OR_DUPLICATE_INSTANCE_TAG")
        found[row["Key"]] = row["Value"]
    return found


class ActorCollector:
    def __init__(self, sdk):
        self.sdk, self.reader = sdk, LegacyCollector(sdk)

    def pages(self, service, operation, parameters, list_key, **kwargs):
        return [row for page in self.reader.pages(service, operation, parameters, list_key, **kwargs) for row in page]

    def profiles(self, roles):
        profiles = {}
        for arn in sorted(roles):
            name = arn.rsplit("/", 1)[-1]
            rows = self.pages("iam", "list_instance_profiles_for_role", dict(RoleName=name), "InstanceProfiles", input_token="Marker", output_token="Marker")
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("Arn"), str) or not PROFILE.fullmatch(row["Arn"])
                        or not isinstance(row.get("InstanceProfileId"), str) or not row["InstanceProfileId"] or not isinstance(row.get("Roles"), list)
                        or not any(isinstance(r, dict) and r.get("Arn") == arn for r in row["Roles"])):
                    raise IncompleteAudit("LEGACY_PROFILE_SCOPE_OR_IDENTITY_UNKNOWN")
                key = row["Arn"]
                if key in profiles and digest(profiles[key]) != digest(row): raise IncompleteAudit("CONFLICTING_LEGACY_PROFILE")
                profiles[key] = row
        return profiles

    def instances(self, session_id, profiles):
        observed, candidates = {}, {}
        reservations = self.pages("ec2", "describe_instances", dict(MaxResults=100), "Reservations", input_token="NextToken", output_token="NextToken")
        for reservation in reservations:
            if not isinstance(reservation, dict) or reservation.get("OwnerId") != ACCOUNT or not isinstance(reservation.get("Instances"), list):
                raise IncompleteAudit("INVALID_EC2_RESERVATION_SCOPE")
            for instance in reservation["Instances"]:
                if not isinstance(instance, dict) or not INSTANCE.fullmatch(str(instance.get("InstanceId", ""))): raise IncompleteAudit("INVALID_INSTANCE_ID")
                iid, state = instance["InstanceId"], instance.get("State", {}).get("Name")
                if iid in observed: raise IncompleteAudit("DUPLICATE_INSTANCE")
                if state not in STATES: raise IncompleteAudit("UNKNOWN_INSTANCE_STATE")
                observed[iid] = instance
                metadata = tags(instance.get("Tags"))
                profile = instance.get("IamInstanceProfile")
                if profile is not None and (not isinstance(profile, dict) or not isinstance(profile.get("Arn"), str) or not PROFILE.fullmatch(profile["Arn"])):
                    raise IncompleteAudit("INSTANCE_PROFILE_SCOPE_UNKNOWN")
                reasons = []
                if profile and profile["Arn"] in profiles:
                    if profile.get("Id") != profiles[profile["Arn"]]["InstanceProfileId"]:
                        raise IncompleteAudit("INSTANCE_PROFILE_RECREATED_OR_CHANGED")
                    reasons.append("actual-legacy-role-profile")
                if metadata.get("SessionId") == session_id: reasons.append("original-session-tag")
                if metadata.get("Project") == "techx":
                    if metadata.get("Environment") == "staging": reasons.append("shared-staging-workload")
                    if metadata.get("Environment") == "automation" and not metadata.get("SessionId", "").startswith("staging-v2-"):
                        reasons.append("automation-without-v2-session")
                if reasons: candidates[iid] = dict(instance=instance, discoveryReasons=reasons)
        return observed, candidates

    def ssm(self, candidates, start):
        params = dict(Filters=[dict(key="InvokedAfter", value=start.isoformat().replace("+00:00", "Z"))], MaxResults=50)
        commands = self.pages("ssm", "list_commands", params, "Commands", input_token="NextToken", output_token="NextToken")
        invocations = self.pages("ssm", "list_command_invocations", dict(params, Details=False), "CommandInvocations", input_token="NextToken", output_token="NextToken")
        nodes = self.pages("ssm", "describe_instance_information", dict(MaxResults=50), "InstanceInformationList", input_token="NextToken", output_token="NextToken")
        by_command, by_invocation, by_node = {}, {}, {}
        for row in commands:
            if not isinstance(row, dict) or not COMMAND.fullmatch(str(row.get("CommandId", ""))) or row.get("Status") not in SSM_STATES:
                raise IncompleteAudit("UNKNOWN_SSM_COMMAND")
            cid = row["CommandId"]
            if cid in by_command: raise IncompleteAudit("DUPLICATE_SSM_COMMAND")
            by_command[cid] = row
        for row in invocations:
            if (not isinstance(row, dict) or not COMMAND.fullmatch(str(row.get("CommandId", "")))
                    or not isinstance(row.get("InstanceId"), str) or row.get("Status") not in SSM_STATES):
                raise IncompleteAudit("UNKNOWN_SSM_INVOCATION")
            key = (row["CommandId"], row["InstanceId"])
            if key in by_invocation: raise IncompleteAudit("DUPLICATE_SSM_INVOCATION")
            by_invocation[key] = row
            if row["CommandId"] not in by_command: raise IncompleteAudit("SSM_COMMAND_HISTORY_CHANGED")
        for row in nodes:
            if not isinstance(row, dict) or not isinstance(row.get("InstanceId"), str) or not isinstance(row.get("PingStatus"), str):
                raise IncompleteAudit("UNKNOWN_SSM_NODE")
            if row["InstanceId"] in by_node: raise IncompleteAudit("DUPLICATE_SSM_NODE")
            by_node[row["InstanceId"]] = row
        relevant_commands, relevant_invocations, unresolved, unscoped_active = [], [], [], []
        for cid, row in by_command.items():
            instances = row.get("InstanceIds", [])
            if not isinstance(instances, list) or any(not isinstance(i, str) for i in instances): raise IncompleteAudit("INVALID_COMMAND_TARGETS")
            document = row.get("DocumentName")
            if not isinstance(document, str): raise IncompleteAudit("MISSING_COMMAND_DOCUMENT")
            related = [v for (c, i), v in by_invocation.items() if c == cid and i in candidates]
            # A TechX document targeting a now-absent instance cannot be ignored.
            techx_document = "techx" in document.lower()
            if set(instances) & set(candidates) or related or techx_document:
                relevant_commands.append(row)
                relevant_invocations.extend(v for (c, _), v in by_invocation.items() if c == cid)
                if techx_document and (not instances or any(i not in candidates for i in instances)):
                    unresolved.append(cid)
            elif row["Status"] not in SSM_TERMINAL:
                unscoped_active.append(cid)  # Unknown target/ownership must remain visible, not silently become zero.
        return dict(commands=relevant_commands, invocations=relevant_invocations,
                    nodes=[row for iid, row in by_node.items() if iid in candidates], unresolvedCommandIds=unresolved,
                    activeCommandIds=[r["CommandId"] for r in relevant_commands if r["Status"] not in SSM_TERMINAL],
                    activeInvocationKeys=[dict(commandId=r["CommandId"], instanceId=r["InstanceId"]) for r in relevant_invocations if r["Status"] not in SSM_TERMINAL],
                    unscopedActiveCommandIds=unscoped_active,
                    unscopedActiveInvocationKeys=[dict(commandId=c, instanceId=i) for (c, i), r in by_invocation.items()
                                                 if i not in candidates and r["Status"] not in SSM_TERMINAL],
                    scannedFrom=wire(start), commandHistoryRetentionComplete=False,
                    limitation="SSM history expires; current offline/absent node is not historical terminal proof")

    def collect(self, session_id, execution_ref):
        if not re.fullmatch(r"staging-gh-[1-9][0-9]{0,19}", session_id): raise IncompleteAudit("INVALID_LEGACY_SESSION")
        identity = self.sdk.call("sts", "get_caller_identity")
        if identity.get("Account") != ACCOUNT: raise IncompleteAudit("WRONG_CALLER_ACCOUNT")
        # Reuse original-byte S3 capture; never derive history bounds from a caller date.
        from migration_v2 import capture_legacy
        from execution_v2 import reference
        reference(execution_ref, key=f"executions/{session_id}.json")
        objects = dict(session=self.reader.object("automation/staging/session.json"),
                       execution=self.reader.object(execution_ref["key"], object_version=execution_ref["versionId"]),
                       pointer=self.reader.object("control/staging/current-execution.json"), inputs=self.reader.object(f"private/staging/{session_id}/inputs.json"))
        snapshot = capture_legacy(objects, session_id=session_id, execution_ref=execution_ref)
        self.reader.unchanged(objects)
        start = service_time(json.loads(objects["execution"]["body"])["createdAt"]) - timedelta(minutes=10)
        if start >= self.sdk.clock(): raise IncompleteAudit("INVALID_ACTOR_HISTORY_INTERVAL")
        authority = AuthorityCollector(self.sdk)
        roles = authority.roles()
        profiles = self.profiles(roles)
        all_instances, candidates = self.instances(session_id, profiles)
        observations = self.ssm(candidates, start)
        builds = self.reader.builds()
        if digest(authority.roles()) != digest(roles) or digest(self.profiles(roles)) != digest(profiles):
            raise IncompleteAudit("ACTOR_ROLE_OR_PROFILE_ROSTER_CHANGED")
        self.reader.unchanged(objects)
        report = dict(collectionStatus="COMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False, actorDrain="NOT_VERIFIED",
                      sessionId=session_id, legacyProfileCount=len(profiles), accountInstanceObservationCount=len(all_instances),
                      candidateInstanceCount=len(candidates), nonterminalCandidateInstanceCount=sum(r["instance"]["State"]["Name"] != "terminated" for r in candidates.values()),
                      relevantCommandCount=len(observations["commands"]), activeCommandCount=len(observations["activeCommandIds"]),
                      activeInvocationCount=len(observations["activeInvocationKeys"]), unresolvedCommandCount=len(observations["unresolvedCommandIds"]),
                      unscopedActiveCommandCount=len(observations["unscopedActiveCommandIds"]),
                      unscopedActiveInvocationCount=len(observations["unscopedActiveInvocationKeys"]),
                      candidateManagedNodeCount=len(observations["nodes"]), activeBuildCount=sum(b["buildStatus"] == "IN_PROGRESS" for p in builds.values() for b in p["builds"]),
                      lambdaInvocationDrain="NOT_OBSERVED", githubManagerDrain="NOT_OBSERVED", historicalEc2TerminalProof="NOT_ESTABLISHED")
        self.sdk.evidence.record(dict(purpose="ACTOR_OBSERVATIONS_NOT_DRAIN_ATTESTATION", observedAt=wire(self.sdk.clock()),
                                      snapshot=snapshot.as_dict(), profiles=profiles, instances=candidates, ssm=observations, builds=builds, report=report))
        self.sdk.evidence.finish(report)
        return ActorObservations(report, self.sdk.evidence.path, _seal=_SEAL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--expected-execution-reference", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--profile")
    args = parser.parse_args(); evidence = PrivateEvidence(args.private_output)
    try:
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        config = Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2})
        clients = {n:session.client(n, region_name=REGION, endpoint_url=u, config=config) for n,u in ENDPOINTS.items()}
        result = ActorCollector(ReadOnlySDK(clients, evidence)).collect(args.session_id, json.loads(args.expected_execution_reference.read_bytes()))
    except Exception as error:
        code = str(error) if isinstance(error, IncompleteAudit) else "INVALID_OR_INCOMPLETE_COLLECTION"
        report = dict(collectionStatus="INCOMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False, reason=code)
        evidence.record(dict(errorType=type(error).__name__, **report)); evidence.finish(report)
        print(json.dumps(dict(report, privateEvidence=str(evidence.path))))
        return 2
    print(json.dumps(dict(result.report, privateEvidence=result.evidence_path)))
    return 0


if __name__ == "__main__": raise SystemExit(main())
