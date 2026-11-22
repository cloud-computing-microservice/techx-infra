from copy import deepcopy
import json
from pathlib import Path
import runpy
import unittest
from urllib.parse import quote

from legacy_authority import AuthorityCollector, AuthorityObservations, policy_document, unconditional_deny_all
from legacy_reconciliation import ACCOUNT, REGION, IncompleteAudit

Helpers = runpy.run_path(str(Path(__file__).with_name("test-legacy-reconciliation.py")))
DENY = dict(Version="2012-10-17", Statement=[dict(Effect="Deny", Action="*", Resource="*")])
TRUST = dict(Version="2012-10-17", Statement=[dict(Effect="Allow", Action="sts:AssumeRole", Principal=dict(Service="lambda.amazonaws.com"))])
ROLE_NAME = "techx-automation-cleanup"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
POLICY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/techx-automation-cleanup"
FUNCTION = "techx-automation-cleanup-launcher"
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FUNCTION}"
RULE = "techx-automation-watchdog"
RULE_ARN = f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{RULE}"


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.base = Helpers["CollectorTests"](); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.sdk, self.evidence = self.base.sdk, self.base.evidence
        self.override = None
        self.base.override = self.respond
        self.collector = AuthorityCollector(self.sdk)
        self.role = dict(RoleName=ROLE_NAME, Arn=ROLE_ARN, RoleId="AROAEXAMPLE", AssumeRolePolicyDocument=deepcopy(TRUST))

    def respond(self, service, operation, params):
        if self.override:
            result = self.override(service, operation, params)
            if result is not None: return result
        if operation == "list_roles":
            return dict(Roles=[dict(RoleName=ROLE_NAME, Arn=ROLE_ARN),
                        dict(RoleName="techx-automation-v2-manager", Arn=f"arn:aws:iam::{ACCOUNT}:role/techx-automation-v2-manager")], IsTruncated=False)
        if operation == "get_role": return dict(Role=deepcopy(self.role))
        if operation == "list_role_policies": return dict(PolicyNames=["retired"], IsTruncated=False)
        if operation == "get_role_policy": return dict(RoleName=ROLE_NAME, PolicyName="retired", PolicyDocument=deepcopy(DENY))
        if operation == "list_attached_role_policies": return dict(AttachedPolicies=[dict(PolicyArn=POLICY_ARN)], IsTruncated=False)
        if operation == "get_policy": return dict(Policy=dict(Arn=POLICY_ARN, DefaultVersionId="v1"))
        if operation == "get_policy_version": return dict(PolicyVersion=dict(VersionId="v1", IsDefaultVersion=True, Document=deepcopy(DENY)))
        if operation == "list_functions": return dict(Functions=[dict(FunctionName=FUNCTION, FunctionArn=FUNCTION_ARN)])
        if operation == "get_function_configuration": return dict(FunctionArn=FUNCTION_ARN, Role=ROLE_ARN, Timeout=60, RevisionId="revision-one")
        if operation == "list_rules": return dict(Rules=[dict(Name=RULE, Arn=RULE_ARN)])
        if operation == "describe_rule": return dict(Arn=RULE_ARN, State="ENABLED")
        if operation == "list_targets_by_rule": return dict(Targets=[dict(Id="launcher", Arn=FUNCTION_ARN)])
        if operation == "list_schedules": return dict(Schedules=[])
        if operation == "list_schedule_groups": return dict(ScheduleGroups=[dict(Name="techx-staging", Arn=f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/techx-staging")])
        # STS is handled by the shared fake SDK fixture.
        return None

    def test_live_sdk_chain_collects_policy_facts_without_attestation(self):
        result = self.collector.collect()
        self.assertEqual(result.report["collectionStatus"], "COMPLETE")
        self.assertEqual(result.report["identityDenyAllObservedCount"], 1)
        self.assertEqual(result.report["trustDenyAllObservedCount"], 0)
        self.assertEqual(result.report["enabledRuleCount"], 1)
        self.assertEqual(result.report["actorDrain"], "NOT_OBSERVED")
        self.assertFalse(result.report["attestationEligible"])
        self.assertFalse(any(p.get("RoleName", "").startswith("techx-automation-v2-") for _, _, p in self.base.calls))

    def test_decoded_operator_result_cannot_construct_observation(self):
        with self.assertRaises(ValueError): AuthorityObservations({}, "private")

    def test_trust_deny_all_requires_unconditional_all_principals(self):
        value = dict(Statement=dict(Effect="Deny", Action="sts:*", Principal="*"))
        self.assertTrue(unconditional_deny_all(value, trust=True))
        for change in (dict(Condition={"StringEquals": {"aws:PrincipalAccount": ACCOUNT}}),
                       dict(Action="sts:AssumeRole"), dict(Principal=dict(AWS=ROLE_ARN)), dict(NotPrincipal="*")):
            altered = deepcopy(value); altered["Statement"].update(change)
            self.assertFalse(unconditional_deny_all(altered, trust=True))

    def test_identity_deny_requires_all_actions_and_resources(self):
        self.assertTrue(unconditional_deny_all(DENY))
        for change in (dict(Effect="Allow"), dict(Action="s3:*"), dict(Resource="arn:aws:s3:::bucket/*"),
                       dict(Condition={}), dict(NotAction="iam:*"), dict(Principal="*")):
            altered = deepcopy(DENY); altered["Statement"][0].update(change)
            self.assertFalse(unconditional_deny_all(altered))

    def test_policy_decodes_percent_encoded_document(self):
        self.assertEqual(policy_document(quote(json.dumps(DENY))), DENY)

    def test_duplicate_and_nonfinite_policy_keys_fail(self):
        for raw in ('{"Statement":[],"Statement":[]}', '{"Statement":[],"x":NaN}'):
            with self.assertRaises(IncompleteAudit): policy_document(raw)

    def test_role_path_or_cross_account_is_not_silently_ignored(self):
        for arn in (ROLE_ARN.replace(ACCOUNT, "000000000000"), ROLE_ARN.replace("role/", "role/path/")):
            self.override = lambda s, o, p: dict(Roles=[dict(RoleName=ROLE_NAME, Arn=arn)], IsTruncated=False) if o == "list_roles" else None
            with self.assertRaisesRegex(IncompleteAudit, "ROLE_SCOPE"): self.collector.collect()

    def test_duplicate_role_roster_fails(self):
        self.override = lambda s, o, p: dict(Roles=[dict(RoleName=ROLE_NAME, Arn=ROLE_ARN)] * 2, IsTruncated=False) if o == "list_roles" else None
        with self.assertRaisesRegex(IncompleteAudit, "DUPLICATE_ROLE"): self.collector.collect()

    def test_wrong_role_id_readback_fails(self):
        self.role["Arn"] = ROLE_ARN.replace(ACCOUNT, "000000000000")
        with self.assertRaisesRegex(IncompleteAudit, "ROLE_IDENTITY"): self.collector.collect()

    def test_managed_policy_default_change_fails(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "get_policy":
                count += 1
                return dict(Policy=dict(Arn=POLICY_ARN, DefaultVersionId="v1" if count == 1 else "v2"))
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "MANAGED_POLICY_CHANGED"): self.collector.collect()

    def test_missing_managed_version_is_not_empty_policy(self):
        self.override = lambda s, o, p: {} if o == "get_policy_version" else None
        with self.assertRaisesRegex(IncompleteAudit, "MANAGED_POLICY_VERSION"): self.collector.collect()

    def test_inline_policy_readback_change_fails(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "get_role_policy":
                count += 1
                doc = deepcopy(DENY)
                if count > 1: doc["Statement"][0]["Effect"] = "Allow"
                return dict(RoleName=ROLE_NAME, PolicyName="retired", PolicyDocument=doc)
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "INLINE_POLICY_CHANGED"): self.collector.collect()

    def test_role_recreated_during_collection_fails(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "get_role":
                count += 1
                role = deepcopy(self.role)
                if count > 1: role["RoleId"] = "AROANEWIDENTITY"
                return dict(Role=role)
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "ROLE_CHANGED"): self.collector.collect()

    def test_missing_inline_list_fails(self):
        self.override = lambda s, o, p: dict(IsTruncated=False) if o == "list_role_policies" else None
        with self.assertRaisesRegex(IncompleteAudit, "EXPLICIT_RESULT_LIST"): self.collector.collect()

    def test_attachment_wrong_account_fails_before_read(self):
        self.override = lambda s, o, p: dict(AttachedPolicies=[dict(PolicyArn=POLICY_ARN.replace(ACCOUNT, "000000000000"))], IsTruncated=False) if o == "list_attached_role_policies" else None
        with self.assertRaisesRegex(IncompleteAudit, "ATTACHED_POLICY_LIST"): self.collector.collect()
        self.assertFalse(any(o == "get_policy" for _, o, _ in self.base.calls))

    def test_permissions_boundary_is_read_at_exact_default_version(self):
        self.role["PermissionsBoundary"] = dict(PermissionsBoundaryType="Policy", PermissionsBoundaryArn=POLICY_ARN)
        self.collector.collect()
        self.assertEqual(sum(o == "get_policy_version" for _, o, _ in self.base.calls), 2)

    def test_function_identity_mismatch_fails(self):
        self.override = lambda s, o, p: dict(FunctionArn="wrong", Role=ROLE_ARN) if o == "get_function_configuration" else None
        with self.assertRaisesRegex(IncompleteAudit, "FUNCTION_CONFIGURATION"): self.collector.collect()

    def test_unknown_rule_state_fails(self):
        self.override = lambda s, o, p: dict(Arn=RULE_ARN, State="UNKNOWN") if o == "describe_rule" else None
        with self.assertRaisesRegex(IncompleteAudit, "RULE_SCOPE_OR_STATE"): self.collector.collect()

    def test_schedule_scope_and_actual_readback(self):
        name = "cleanup-staging-gh-1001"
        arn = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/techx-staging/{name}"
        def override(s, o, p):
            if o == "list_schedules": return dict(Schedules=[dict(Name=name, GroupName="techx-staging", Arn=arn)])
            if o == "get_schedule":
                self.assertEqual(p, dict(GroupName="techx-staging", Name=name))
                return dict(Arn=arn, State="ENABLED")
        self.override = override
        result = self.collector.collect()
        self.assertEqual(result.report["enabledScheduleCount"], 1)
        self.assertFalse(result.report["attestationEligible"])

    def test_new_role_appearing_changes_roster(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "list_roles":
                count += 1
                return dict(Roles=[dict(RoleName=ROLE_NAME, Arn=ROLE_ARN)] if count == 1 else [], IsTruncated=False)
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "ROLE_ROSTER_CHANGED"): self.collector.collect()

    def test_role_same_arn_recreated_after_detail_reads_changes_roster(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "list_roles":
                count += 1
                return dict(Roles=[dict(RoleName=ROLE_NAME, Arn=ROLE_ARN, RoleId="old" if count == 1 else "new")], IsTruncated=False)
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "ROLE_ROSTER_CHANGED"): self.collector.collect()

    def test_duplicate_trigger_rosters_are_incomplete(self):
        for operation, value in (("list_functions", dict(Functions=[dict(FunctionName=FUNCTION, FunctionArn=FUNCTION_ARN)] * 2)),
                                 ("list_rules", dict(Rules=[dict(Name=RULE, Arn=RULE_ARN)] * 2))):
            self.override = lambda s, o, p: deepcopy(value) if o == operation else None
            with self.assertRaisesRegex(IncompleteAudit, "DUPLICATE_"): self.collector.collect()

    def test_absent_group_uses_positive_complete_list_not_an_error(self):
        self.override = lambda s, o, p: dict(ScheduleGroups=[dict(Name="default")]) if o == "list_schedule_groups" else None
        result = self.collector.collect()
        self.assertEqual(result.report["scheduleGroupObservation"], "ABSENT_FROM_POSITIVE_PAGINATED_LIST")
        self.assertFalse(any(o == "list_schedules" for _, o, _ in self.base.calls))

    def test_missing_schedule_group_list_is_not_absence(self):
        self.override = lambda s, o, p: {} if o == "list_schedule_groups" else None
        with self.assertRaisesRegex(IncompleteAudit, "EXPLICIT_RESULT_LIST"): self.collector.collect()

    def test_schedule_group_appears_during_capture_fails(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "list_schedule_groups":
                count += 1
                if count == 1: return dict(ScheduleGroups=[])
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "SCHEDULE_GROUP_CHANGED"): self.collector.collect()


if __name__ == "__main__": unittest.main()

