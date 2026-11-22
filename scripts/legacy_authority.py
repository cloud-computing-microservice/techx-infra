"""Read-only authority observations for reviewed v1 retirement planning.

Own live SDK reads are required. These are observations, never a migration
closeout, retirement attestation, actor-drain proof or staging admission.
"""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

from execution_v2 import ACCOUNT, REGION
from legacy_reconciliation import (ENDPOINTS, IncompleteAudit, LegacyCollector, PrivateEvidence, ReadOnlySDK, encoded, wire)

ROLE = re.compile(rf"arn:aws:iam::{ACCOUNT}:role/techx-automation-(?!v2(?:-|$))[a-z0-9-]+")
NAME = re.compile(r"techx-automation-(?!v2(?:-|$))[a-z0-9-]+")
POLICY = re.compile(rf"arn:aws:iam::(?:{ACCOUNT}|aws):policy/[A-Za-z0-9+=,.@_/-]+")
SCHEDULE = re.compile(r"[a-zA-Z0-9-]+")
_SEAL = object()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def policy_document(value):
    if isinstance(value, str):
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result: raise IncompleteAudit("DUPLICATE_POLICY_KEY")
                result[key] = item
            return result
        def constant(_): raise IncompleteAudit("NONFINITE_POLICY_VALUE")
        value = json.loads(unquote(value), object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(value, dict): raise IncompleteAudit("INVALID_POLICY_DOCUMENT")
    statements = value.get("Statement")
    if isinstance(statements, dict): statements = [statements]
    if not isinstance(statements, list) or not statements or any(not isinstance(s, dict) for s in statements):
        raise IncompleteAudit("INVALID_POLICY_STATEMENTS")
    return deepcopy(value)


def unconditional_deny_all(value, *, trust=False):
    """Narrow structural observation; never evaluate IAM from a few sample actions."""
    doc = policy_document(value)
    statements = doc["Statement"]
    if isinstance(statements, dict): statements = [statements]
    for statement in statements:
        allowed_keys = {"Sid", "Effect", "Action", "Principal"} if trust else {"Sid", "Effect", "Action", "Resource"}
        if set(statement) - allowed_keys or statement.get("Effect") != "Deny": continue
        actions = statement.get("Action")
        if isinstance(actions, str): actions = [actions]
        if not isinstance(actions, list) or not any(a in (("*", "sts:*") if trust else ("*",)) for a in actions): continue
        if trust:
            if statement.get("Principal") == "*": return True
        else:
            resource = statement.get("Resource")
            if resource == "*" or resource == ["*"]: return True
    return False


class AuthorityObservations:
    __slots__ = ("report", "evidence_path")

    def __init__(self, report, path, *, _seal=None):
        if _seal is not _SEAL: raise ValueError("Use the authenticated authority collector")
        self.report, self.evidence_path = deepcopy(report), str(path)


class AuthorityCollector:
    def __init__(self, sdk):
        self.sdk = sdk
        self.reader = LegacyCollector(sdk)

    def pages(self, service, operation, parameters, list_key, **kwargs):
        return [row for page in self.reader.pages(service, operation, parameters, list_key, **kwargs) for row in page]

    def roles(self):
        rows = self.pages("iam", "list_roles", {}, "Roles", input_token="Marker", output_token="Marker")
        found = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("RoleName"), str): raise IncompleteAudit("INVALID_ROLE_LIST")
            name = row["RoleName"]
            if not name.startswith("techx-automation-") or re.match(r"techx-automation-v2(?:-|$)", name): continue
            arn = row.get("Arn")
            if not NAME.fullmatch(name) or not isinstance(arn, str) or not ROLE.fullmatch(arn) or arn.rsplit("/", 1)[-1] != name:
                raise IncompleteAudit("UNEXPECTED_LEGACY_ROLE_SCOPE")
            if arn in found: raise IncompleteAudit("DUPLICATE_ROLE")
            found[arn] = row
            if len(found) > 128: raise IncompleteAudit("LEGACY_ROLE_BOUND_EXCEEDED")
        return found

    def role_identity(self, name, arn):
        result = self.sdk.call("iam", "get_role", RoleName=name).get("Role")
        if (not isinstance(result, dict) or result.get("RoleName") != name or result.get("Arn") != arn
                or not isinstance(result.get("RoleId"), str) or not result["RoleId"]):
            raise IncompleteAudit("ROLE_IDENTITY_MISMATCH")
        policy_document(result.get("AssumeRolePolicyDocument"))
        return result

    def role_lists(self, name):
        inline = self.pages("iam", "list_role_policies", dict(RoleName=name), "PolicyNames", input_token="Marker", output_token="Marker")
        attached = self.pages("iam", "list_attached_role_policies", dict(RoleName=name), "AttachedPolicies", input_token="Marker", output_token="Marker")
        if len(inline) > 128 or any(not isinstance(n, str) or not re.fullmatch(r"[\w+=,.@-]{1,128}", n) for n in inline) or len(inline) != len(set(inline)):
            raise IncompleteAudit("INVALID_INLINE_POLICY_LIST")
        arns = [r.get("PolicyArn") for r in attached if isinstance(r, dict)]
        if len(attached) > 64 or len(arns) != len(attached) or any(not isinstance(a, str) or not POLICY.fullmatch(a) for a in arns) or len(set(arns)) != len(arns):
            raise IncompleteAudit("INVALID_ATTACHED_POLICY_LIST")
        return sorted(inline), sorted(arns)

    def managed(self, arn):
        policy = self.sdk.call("iam", "get_policy", PolicyArn=arn).get("Policy")
        if not isinstance(policy, dict) or policy.get("Arn") != arn or not re.fullmatch(r"v[1-9][0-9]*", str(policy.get("DefaultVersionId", ""))):
            raise IncompleteAudit("INVALID_MANAGED_POLICY_IDENTITY")
        version = policy["DefaultVersionId"]
        data = self.sdk.call("iam", "get_policy_version", PolicyArn=arn, VersionId=version).get("PolicyVersion")
        if not isinstance(data, dict) or data.get("VersionId") != version or data.get("IsDefaultVersion") is not True:
            raise IncompleteAudit("MANAGED_POLICY_VERSION_CHANGED")
        document = policy_document(data.get("Document"))
        after = self.sdk.call("iam", "get_policy", PolicyArn=arn).get("Policy")
        if digest(after) != digest(policy): raise IncompleteAudit("MANAGED_POLICY_CHANGED_DURING_CAPTURE")
        return dict(arn=arn, versionId=version, document=document, documentSha256=digest(document))

    def role(self, arn):
        name = arn.rsplit("/", 1)[-1]
        before = self.role_identity(name, arn)
        names, arns = self.role_lists(name)
        inline, attached = [], []
        for policy_name in names:
            data = self.sdk.call("iam", "get_role_policy", RoleName=name, PolicyName=policy_name)
            if data.get("RoleName") != name or data.get("PolicyName") != policy_name: raise IncompleteAudit("INLINE_POLICY_IDENTITY_MISMATCH")
            doc = policy_document(data.get("PolicyDocument"))
            inline.append(dict(name=policy_name, document=doc, documentSha256=digest(doc)))
        for policy_arn in arns: attached.append(self.managed(policy_arn))
        boundary = before.get("PermissionsBoundary")
        boundary_observation = None
        if boundary is not None:
            if (not isinstance(boundary, dict) or boundary.get("PermissionsBoundaryType") != "Policy"
                    or not isinstance(boundary.get("PermissionsBoundaryArn"), str) or not POLICY.fullmatch(boundary["PermissionsBoundaryArn"])):
                raise IncompleteAudit("INVALID_BOUNDARY")
            boundary_observation = self.managed(boundary["PermissionsBoundaryArn"])
        if self.role_lists(name) != (names, arns) or digest(self.role_identity(name, arn)) != digest(before):
            raise IncompleteAudit("ROLE_CHANGED_DURING_CAPTURE")
        # Inline policies have no immutable version ID; read their content twice.
        for item in inline:
            after = self.sdk.call("iam", "get_role_policy", RoleName=name, PolicyName=item["name"])
            if (after.get("RoleName") != name or after.get("PolicyName") != item["name"]
                    or digest(policy_document(after.get("PolicyDocument"))) != item["documentSha256"]):
                raise IncompleteAudit("INLINE_POLICY_CHANGED_DURING_CAPTURE")
        return dict(arn=arn, role=before, inlinePolicies=inline, attachedPolicies=attached, permissionsBoundary=boundary_observation,
                    unconditionalIdentityDenyAllObserved=any(unconditional_deny_all(p["document"]) for p in inline + attached),
                    unconditionalTrustDenyAllObserved=unconditional_deny_all(before["AssumeRolePolicyDocument"], trust=True))

    def triggers(self):
        functions, seen_functions = [], set()
        for row in self.pages("lambda", "list_functions", {}, "Functions", input_token="Marker", output_token="NextMarker"):
            if not isinstance(row, dict) or not isinstance(row.get("FunctionName"), str): raise IncompleteAudit("INVALID_FUNCTION_LIST")
            name = row["FunctionName"]
            if not NAME.fullmatch(name):
                if name.startswith("techx-automation-") and not re.match(r"techx-automation-v2(?:-|$)", name): raise IncompleteAudit("UNEXPECTED_LEGACY_FUNCTION_SCOPE")
                continue
            arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"
            if row.get("FunctionArn") != arn: raise IncompleteAudit("FUNCTION_SCOPE_MISMATCH")
            if arn in seen_functions: raise IncompleteAudit("DUPLICATE_FUNCTION")
            seen_functions.add(arn)
            data = self.sdk.call("lambda", "get_function_configuration", FunctionName=arn)
            if data.get("FunctionArn") != arn or not isinstance(data.get("Role"), str): raise IncompleteAudit("FUNCTION_CONFIGURATION_MISMATCH")
            functions.append(data)
        rules, seen_rules = [], set()
        for row in self.pages("events", "list_rules", dict(NamePrefix="techx-automation-"), "Rules", input_token="NextToken", output_token="NextToken"):
            if not isinstance(row, dict) or not isinstance(row.get("Name"), str): raise IncompleteAudit("INVALID_RULE_LIST")
            name = row["Name"]
            if re.match(r"techx-automation-v2(?:-|$)", name): continue
            if not NAME.fullmatch(name): raise IncompleteAudit("UNEXPECTED_LEGACY_RULE_SCOPE")
            arn = f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{name}"
            if row.get("Arn") != arn: raise IncompleteAudit("RULE_LIST_SCOPE_MISMATCH")
            if arn in seen_rules: raise IncompleteAudit("DUPLICATE_RULE")
            seen_rules.add(arn)
            data = self.sdk.call("events", "describe_rule", Name=name)
            if data.get("Arn") != arn or data.get("State") not in ("ENABLED", "DISABLED", "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"):
                raise IncompleteAudit("RULE_SCOPE_OR_STATE_UNKNOWN")
            targets = self.pages("events", "list_targets_by_rule", dict(Rule=name), "Targets", input_token="NextToken", output_token="NextToken")
            rules.append(dict(configuration=data, targets=targets))
        schedules, seen_schedules = [], set()
        def group_present():
            groups = self.pages("scheduler", "list_schedule_groups", {}, "ScheduleGroups", input_token="NextToken", output_token="NextToken")
            matches = [g for g in groups if isinstance(g, dict) and g.get("Name") == "techx-staging"]
            if any(not isinstance(g, dict) or not isinstance(g.get("Name"), str) for g in groups) or len(matches) > 1:
                raise IncompleteAudit("INVALID_SCHEDULE_GROUP_LIST")
            if matches and matches[0].get("Arn") != f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/techx-staging":
                raise IncompleteAudit("SCHEDULE_GROUP_SCOPE_MISMATCH")
            return bool(matches)
        present = group_present()
        rows = self.pages("scheduler", "list_schedules", dict(GroupName="techx-staging"), "Schedules", input_token="NextToken", output_token="NextToken") if present else []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("Name"), str): raise IncompleteAudit("INVALID_SCHEDULE_LIST")
            name = row["Name"]
            if re.match(r"(?:v2-|staging-v2-|cleanup-staging-v2-)", name): continue
            if not SCHEDULE.fullmatch(name) or row.get("GroupName") != "techx-staging": raise IncompleteAudit("SCHEDULE_SCOPE_MISMATCH")
            arn = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/techx-staging/{name}"
            if row.get("Arn") != arn: raise IncompleteAudit("SCHEDULE_LIST_SCOPE_MISMATCH")
            if arn in seen_schedules: raise IncompleteAudit("DUPLICATE_SCHEDULE")
            seen_schedules.add(arn)
            data = self.sdk.call("scheduler", "get_schedule", GroupName="techx-staging", Name=name)
            if data.get("Arn") != arn or data.get("State") not in ("ENABLED", "DISABLED"): raise IncompleteAudit("SCHEDULE_SCOPE_OR_STATE_UNKNOWN")
            schedules.append(data)
        if group_present() != present: raise IncompleteAudit("SCHEDULE_GROUP_CHANGED_DURING_CAPTURE")
        return dict(functions=functions, rules=rules, schedules=schedules,
                    scheduleGroupObservation="PRESENT" if present else "ABSENT_FROM_POSITIVE_PAGINATED_LIST",
                    actorDrain="NOT_OBSERVED", limitation="Disabled triggers or deny policies do not prove in-flight actors terminal")

    def collect(self):
        identity = self.sdk.call("sts", "get_caller_identity")
        if identity.get("Account") != ACCOUNT: raise IncompleteAudit("WRONG_CALLER_ACCOUNT")
        initial = self.roles()
        roles = [self.role(arn) for arn in sorted(initial)]
        triggers = self.triggers()
        if digest(self.roles()) != digest(initial): raise IncompleteAudit("ROLE_ROSTER_CHANGED_DURING_CAPTURE")
        report = dict(collectionStatus="COMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False,
                      roleCount=len(roles), identityDenyAllObservedCount=sum(r["unconditionalIdentityDenyAllObserved"] for r in roles),
                      trustDenyAllObservedCount=sum(r["unconditionalTrustDenyAllObserved"] for r in roles),
                      functionCount=len(triggers["functions"]), ruleCount=len(triggers["rules"]), scheduleCount=len(triggers["schedules"]),
                      enabledRuleCount=sum(r["configuration"]["State"] != "DISABLED" for r in triggers["rules"]),
                      enabledScheduleCount=sum(s["State"] == "ENABLED" for s in triggers["schedules"]), actorDrain="NOT_OBSERVED")
        report["scheduleGroupObservation"] = triggers["scheduleGroupObservation"]
        self.sdk.evidence.record(dict(purpose="AUTHORITY_OBSERVATIONS_NOT_RETIREMENT_ATTESTATION", account=ACCOUNT, region=REGION,
                                      observedAt=wire(self.sdk.clock()), roles=roles, triggers=triggers, report=report))
        self.sdk.evidence.finish(report)
        return AuthorityObservations(report, self.sdk.evidence.path, _seal=_SEAL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    evidence = PrivateEvidence(args.private_output)
    try:
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        config = Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2})
        clients = {n:session.client(n, region_name=REGION, endpoint_url=u, config=config) for n,u in ENDPOINTS.items()}
        result = AuthorityCollector(ReadOnlySDK(clients, evidence)).collect()
    except Exception as error:
        code = str(error) if isinstance(error, IncompleteAudit) else "INVALID_OR_INCOMPLETE_COLLECTION"
        report = dict(collectionStatus="INCOMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False, reason=code)
        evidence.record(dict(errorType=type(error).__name__, **report)); evidence.finish(report)
        print(json.dumps(dict(report, privateEvidence=str(evidence.path))))
        return 2
    print(json.dumps(dict(result.report, privateEvidence=result.evidence_path)))
    return 0


if __name__ == "__main__": raise SystemExit(main())

