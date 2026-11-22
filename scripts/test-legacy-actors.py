from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import runpy
import unittest

from legacy_actors import ActorCollector, ActorObservations, tags
from legacy_reconciliation import ACCOUNT, IncompleteAudit

Helpers = runpy.run_path(str(Path(__file__).with_name("test-legacy-reconciliation.py")))
ROLE_NAME = "techx-automation-deployment-executor"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
PROFILE_ARN = f"arn:aws:iam::{ACCOUNT}:instance-profile/unexpected-name-still-bound-to-legacy-role"
IID = "i-1234567890abcdef0"
CID = "12345678-1234-1234-1234-1234567890ab"


class ActorTests(unittest.TestCase):
    def setUp(self):
        self.base = Helpers["CollectorTests"](); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.sdk, self.fixture = self.base.sdk, self.base.fixture
        self.override = None
        self.base.override = self.respond
        self.collector = ActorCollector(self.sdk)
        self.profile = dict(Arn=PROFILE_ARN, InstanceProfileId="AIPAEXAMPLE", Roles=[dict(Arn=ROLE_ARN)])
        self.instance = dict(InstanceId=IID, State=dict(Name="running"), IamInstanceProfile=dict(Arn=PROFILE_ARN, Id="AIPAEXAMPLE"))

    def respond(self, service, operation, params):
        if self.override:
            value = self.override(service, operation, params)
            if value is not None: return value
        if operation == "list_roles": return dict(Roles=[dict(RoleName=ROLE_NAME, Arn=ROLE_ARN)], IsTruncated=False)
        if operation == "list_instance_profiles_for_role": return dict(InstanceProfiles=[deepcopy(self.profile)], IsTruncated=False)
        if operation == "describe_instances": return dict(Reservations=[dict(OwnerId=ACCOUNT, Instances=[deepcopy(self.instance)])])
        if operation == "list_commands": return dict(Commands=[])
        if operation == "list_command_invocations": return dict(CommandInvocations=[])
        if operation == "describe_instance_information": return dict(InstanceInformationList=[])
        return None

    def collect(self): return self.collector.collect(self.fixture.sid, self.fixture.legacy_ref)

    def test_profile_discovery_does_not_depend_on_session_or_profile_name(self):
        result = self.collect()
        self.assertEqual(result.report["candidateInstanceCount"], 1)
        self.assertEqual(result.report["nonterminalCandidateInstanceCount"], 1)
        self.assertEqual(result.report["actorDrain"], "NOT_VERIFIED")
        self.assertEqual(result.report["historicalEc2TerminalProof"], "NOT_ESTABLISHED")
        self.assertFalse(result.report["attestationEligible"])

    def test_operator_json_cannot_construct_actor_observations(self):
        with self.assertRaises(ValueError): ActorObservations({}, "private")

    def test_all_staging_instances_included_without_legacy_profile(self):
        self.instance.pop("IamInstanceProfile")
        self.instance["Tags"] = [dict(Key="Project", Value="techx"), dict(Key="Environment", Value="staging")]
        self.assertEqual(self.collect().report["candidateInstanceCount"], 1)

    def test_original_session_tag_included_without_project_tag(self):
        self.instance.pop("IamInstanceProfile")
        self.instance["Tags"] = [dict(Key="SessionId", Value=self.fixture.sid)]
        self.assertEqual(self.collect().report["candidateInstanceCount"], 1)

    def test_untagged_control_instance_included_when_automation_owned(self):
        self.instance.pop("IamInstanceProfile")
        self.instance["Tags"] = [dict(Key="Project", Value="techx"), dict(Key="Environment", Value="automation")]
        self.assertEqual(self.collect().report["candidateInstanceCount"], 1)

    def test_explicit_v2_automation_is_not_counted_as_legacy(self):
        self.instance.pop("IamInstanceProfile")
        self.instance["Tags"] = [dict(Key="Project", Value="techx"), dict(Key="Environment", Value="automation"), dict(Key="SessionId", Value="staging-v2-new")]
        self.assertEqual(self.collect().report["candidateInstanceCount"], 0)

    def test_empty_account_is_observation_not_terminal_proof(self):
        self.override = lambda s, o, p: dict(Reservations=[]) if o == "describe_instances" else None
        result = self.collect()
        self.assertEqual(result.report["candidateInstanceCount"], 0)
        self.assertEqual(result.report["actorDrain"], "NOT_VERIFIED")

    def test_stopped_instance_is_nonterminal(self):
        self.instance["State"]["Name"] = "stopped"
        self.assertEqual(self.collect().report["nonterminalCandidateInstanceCount"], 1)

    def test_terminated_instance_does_not_establish_complete_historical_drain(self):
        self.instance["State"]["Name"] = "terminated"
        result = self.collect()
        self.assertEqual(result.report["nonterminalCandidateInstanceCount"], 0)
        self.assertEqual(result.report["actorDrain"], "NOT_VERIFIED")

    def test_wrong_reservation_owner_fails(self):
        self.override = lambda s, o, p: dict(Reservations=[dict(OwnerId="000000000000", Instances=[])]) if o == "describe_instances" else None
        with self.assertRaisesRegex(IncompleteAudit, "RESERVATION_SCOPE"): self.collect()

    def test_unknown_state_fails(self):
        self.instance["State"]["Name"] = "unknown"
        with self.assertRaisesRegex(IncompleteAudit, "UNKNOWN_INSTANCE_STATE"): self.collect()

    def test_recreated_profile_with_same_arn_fails(self):
        self.instance["IamInstanceProfile"]["Id"] = "AIPANEW"
        with self.assertRaisesRegex(IncompleteAudit, "PROFILE_RECREATED"): self.collect()

    def test_missing_profile_list_is_not_no_profiles(self):
        self.override = lambda s, o, p: dict(IsTruncated=False) if o == "list_instance_profiles_for_role" else None
        with self.assertRaisesRegex(IncompleteAudit, "EXPLICIT_RESULT_LIST"): self.collect()

    def test_profile_bound_to_wrong_role_fails(self):
        self.profile["Roles"] = [dict(Arn=ROLE_ARN + "-other")]
        with self.assertRaisesRegex(IncompleteAudit, "PROFILE_SCOPE"): self.collect()

    def test_profile_roster_changes_fail(self):
        count = 0
        def override(s, o, p):
            nonlocal count
            if o == "list_instance_profiles_for_role":
                count += 1
                if count > 1: return dict(InstanceProfiles=[], IsTruncated=False)
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "PROFILE_ROSTER_CHANGED"): self.collect()

    def test_duplicate_tags_are_not_collapsed(self):
        with self.assertRaisesRegex(IncompleteAudit, "DUPLICATE_INSTANCE_TAG"):
            tags([dict(Key="SessionId", Value="one"), dict(Key="SessionId", Value="two")])

    def test_command_and_invocation_running_are_reported(self):
        def override(s, o, p):
            if o == "list_commands": return dict(Commands=[dict(CommandId=CID, DocumentName="AWS-RunShellScript", InstanceIds=[IID], Status="InProgress")])
            if o == "list_command_invocations": return dict(CommandInvocations=[dict(CommandId=CID, InstanceId=IID, Status="InProgress")])
            if o == "describe_instance_information": return dict(InstanceInformationList=[dict(InstanceId=IID, PingStatus="ConnectionLost")])
        self.override = override
        report = self.collect().report
        self.assertEqual((report["activeCommandCount"], report["activeInvocationCount"], report["candidateManagedNodeCount"]), (1, 1, 1))
        self.assertEqual(report["actorDrain"], "NOT_VERIFIED")

    def test_techx_command_with_absent_instance_is_unresolved(self):
        self.override = lambda s, o, p: dict(Commands=[dict(CommandId=CID, DocumentName="TechX-TypedTask", InstanceIds=["i-abcdef01234567890"], Status="Success")]) if o == "list_commands" else None
        report = self.collect().report
        self.assertEqual(report["unresolvedCommandCount"], 1)
        self.assertEqual(report["relevantCommandCount"], 1)

    def test_invocation_without_command_is_history_race_not_success(self):
        self.override = lambda s, o, p: dict(CommandInvocations=[dict(CommandId=CID, InstanceId=IID, Status="Success")]) if o == "list_command_invocations" else None
        with self.assertRaisesRegex(IncompleteAudit, "COMMAND_HISTORY_CHANGED"): self.collect()

    def test_missing_ssm_list_is_not_zero(self):
        self.override = lambda s, o, p: {} if o == "list_commands" else None
        with self.assertRaisesRegex(IncompleteAudit, "EXPLICIT_RESULT_LIST"): self.collect()

    def test_unknown_command_status_is_not_terminal(self):
        self.override = lambda s, o, p: dict(Commands=[dict(CommandId=CID, DocumentName="TechX-Task", Status="UNKNOWN")]) if o == "list_commands" else None
        with self.assertRaisesRegex(IncompleteAudit, "UNKNOWN_SSM_COMMAND"): self.collect()

    def test_history_interval_comes_from_bound_original_execution(self):
        self.collect()
        calls = [p for s, o, p in self.base.calls if o == "list_commands"]
        self.assertEqual(calls[0]["Filters"], [dict(key="InvokedAfter", value="2026-11-09T13:42:00Z")])

    def test_unscoped_nonterminal_commands_and_invocations_remain_visible(self):
        def override(s, o, p):
            if o == "list_commands": return dict(Commands=[dict(CommandId=CID, DocumentName="AWS-RunShellScript", Targets=[dict(Key="tag:Name", Values=["unknown"])], Status="InProgress")])
            if o == "list_command_invocations": return dict(CommandInvocations=[dict(CommandId=CID, InstanceId="i-abcdef01234567890", Status="InProgress")])
        self.override = override
        report = self.collect().report
        self.assertEqual(report["unscopedActiveCommandCount"], 1)
        self.assertEqual(report["unscopedActiveInvocationCount"], 1)
        self.assertEqual(report["actorDrain"], "NOT_VERIFIED")


if __name__ == "__main__": unittest.main()
