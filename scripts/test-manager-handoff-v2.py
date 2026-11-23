from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

from execution_v2 import ACCOUNT, PREFIX, REGION
from manager_bridge_v2 import AuthenticationFailed, BINDINGS, CLEANUP, LIFECYCLE, REPO, VerifiedManager
from manager_handoff_v2 import (HandoffRejected, HandoffUncertain, SensitiveHandoff, ServerManagerHandoff,
                                 VerifiedHandoff, build_handoff, invoke_handoff, observation_ack, request_id)
from session_runtime_v2 import encoded

Helpers = runpy.run_path(str(Path(__file__).with_name("test-manager-bridge-v2.py")))


class ReceiverSTS:
    def __init__(self, arn):
        name = arn.rsplit(":",1)[-1]
        self.caller = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/{name}/{name}")
        self.meta = SimpleNamespace(endpoint_url=f"https://sts.{REGION}.amazonaws.com", region_name=REGION,
                                    config=SimpleNamespace(retries={"total_max_attempts":1}))
    def get_caller_identity(self):
        return dict(self.caller, ResponseMetadata=dict(HTTPStatusCode=200, RequestId="receiver-sts-request"))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = Helpers["BridgeTests"](); self.f.setUp()
        self.manager = self.f.authenticate()
        sid = self.f.aws.execution["sessionId"]
        self.intent = dict(bucket=self.f.aws.ref["bucket"], key=f"{PREFIX}launches/{sid}/deployment/1/intent.json", versionId="intent-version", sha256="d"*64)
        self.receiver_sts = ReceiverSTS(LIFECYCLE)
        self.receiver = ServerManagerHandoff(self.f.bridge, self.receiver_sts, function_arn=LIFECYCLE)
        self.context = SimpleNamespace(invoked_function_arn=LIFECYCLE, aws_request_id="lambda-invocation-one")

    def packet(self): return build_handoff(self.manager, self.f.token, operation="launch-deployment", intent_reference=self.intent)

    def event(self, packet=None): return json.loads((packet or self.packet())._payload)

    def rehash(self, event):
        event["request"]["requestId"] = request_id(event["request"])
        return event

    def test_receiver_independently_verifies_jwt_and_only_read_only_credentials(self):
        packet = self.packet(); before = len(self.f.exchanges)
        result = self.receiver.verify(self.event(packet), self.context)
        self.assertEqual(len(self.f.exchanges)-before, 1)
        bootstrap = json.loads(self.f.exchanges[-1]["Policy"])
        self.assertNotIn("ssm:SendCommand", encoded(bootstrap).decode())
        self.assertNotIn("lambda:InvokeFunction", encoded(bootstrap).decode())
        self.assertEqual(result.as_dict()["admission"], "NOT_ESTABLISHED")
        self.assertIsNotNone(result.require_current())
        self.assertEqual(self.f.aws.puts(), [])
        for diagnostic in (repr(packet), encoded(packet.as_dict()).decode(), encoded(result.as_dict()).decode()):
            self.assertNotIn(self.f.token, diagnostic)
            self.assertNotIn("never-log-secret-key", diagnostic)

    def test_wire_or_json_cannot_construct_authenticated_handoff(self):
        with self.assertRaises(HandoffRejected): SensitiveHandoff({}, "token")
        with self.assertRaises(HandoffRejected): VerifiedHandoff({}, {}, None, "id")
        with self.assertRaises(HandoffRejected): build_handoff(self.manager.as_dict(), self.f.token, operation="observe")
        event = self.event(); event["callerIdentity"] = self.manager.as_dict()
        with self.assertRaises(HandoffRejected): self.receiver.verify(event, self.context)

    def test_missing_or_wrong_signature_cannot_use_sender_identity_json(self):
        event = self.event(); event["oidcToken"] = json.dumps(self.manager.as_dict())
        with self.assertRaises(AuthenticationFailed): self.receiver.verify(event, self.context)
        self.f.sts_error = True
        with self.assertRaises(AuthenticationFailed): self.receiver.verify(self.event(), self.context)

    def test_actual_receiver_role_and_invocation_required(self):
        self.receiver_sts.caller["Arn"] = self.f.aws.principal
        with self.assertRaises(HandoffRejected): self.receiver.verify(self.event(), self.context)
        self.receiver_sts = ReceiverSTS(LIFECYCLE)
        self.receiver = ServerManagerHandoff(self.f.bridge, self.receiver_sts, function_arn=LIFECYCLE)
        with self.assertRaises(HandoffRejected): self.receiver.verify(self.event(), SimpleNamespace(invoked_function_arn=CLEANUP, aws_request_id="id"))

    def test_wrong_receiver_for_operation_is_rejected(self):
        receiver = ServerManagerHandoff(self.f.bridge, ReceiverSTS(CLEANUP), function_arn=CLEANUP)
        context = SimpleNamespace(invoked_function_arn=CLEANUP, aws_request_id="id")
        with self.assertRaises(HandoffRejected): receiver.verify(self.event(), context)

    def test_request_digest_cannot_hide_changes(self):
        event = self.event(); event["request"]["leaseEpoch"] = 2
        with self.assertRaises(HandoffRejected): self.receiver.verify(event, self.context)

    def test_intent_scope_and_job_cannot_select_arbitrary_worker_config(self):
        for change in (dict(intentReference=dict(self.intent,key="other-project/intent.json")), dict(operation="run-shell"), dict(leaseEpoch=True),
                       dict(job="approve")):
            event = self.event(); event["request"].update(change)
            with self.assertRaises((ValueError, HandoffRejected)): self.receiver.verify(self.rehash(event), self.context)

    def test_stale_epoch_cannot_authorize_mutation(self):
        event = self.event()
        self.f.aws.publish_session(dict(self.f.aws.record, status="DESTROYING", leaseEpoch=2, runtimeRevision=2, lastMutationId="f"*32))
        with self.assertRaises((HandoffRejected,AuthenticationFailed)): self.receiver.verify(event, self.context)

    def test_observation_can_keep_original_epoch_after_fencing(self):
        self.f.aws.publish_session(dict(self.f.aws.record, status="DESTROYING", leaseEpoch=2, runtimeRevision=2, lastMutationId="f"*32))
        packet = build_handoff(self.manager, self.f.token, operation="observe", intent_reference=self.intent, observe_epoch=1)
        result = self.receiver.verify(self.event(packet), self.context)
        self.assertEqual(result.require_current().as_dict()["leaseEpoch"], 2)
        self.assertEqual(result.as_dict()["request"]["leaseEpoch"], 1)

    def test_job_cancellation_after_server_verification_blocks_consumption(self):
        verified = self.receiver.verify(self.event(), self.context)
        self.f.run.update(status="completed", conclusion="cancelled")
        with self.assertRaises(AuthenticationFailed): verified.require_current()

    def test_fence_after_server_verification_blocks_consumption(self):
        verified = self.receiver.verify(self.event(), self.context)
        self.f.aws.publish_session(dict(self.f.aws.record, status="DESTROYING", leaseEpoch=2, runtimeRevision=2, lastMutationId="f"*32))
        with self.assertRaises((HandoffRejected,AuthenticationFailed)): verified.require_current()

    def test_job_scope_rejects_unauthorized_launch_purpose(self):
        with self.assertRaises(HandoffRejected): build_handoff(self.manager,self.f.token, operation="arm-cleanup", intent_reference=self.intent)

    def test_historical_epoch_cannot_be_used_for_mutation(self):
        with self.assertRaises(HandoffRejected): build_handoff(self.manager,self.f.token, operation="launch-deployment", intent_reference=self.intent, observe_epoch=1)

    def test_request_cleanup_cannot_carry_launch_intent(self):
        with self.assertRaises(HandoffRejected): build_handoff(self.manager,self.f.token, operation="request-cleanup", intent_reference=self.intent)

    def test_fresh_token_must_belong_to_original_manager_run(self):
        old = self.f.claims["run_id"]; self.f.claims_changed(run_id="999999")
        with self.assertRaises(HandoffRejected): self.packet()
        self.f.claims_changed(run_id=old)

    def test_receiver_never_returns_raw_token_or_operational_clients(self):
        verified = self.receiver.verify(self.event(), self.context)
        self.assertIsInstance(verified._manager, VerifiedManager)
        self.assertFalse(hasattr(verified._manager,"client"))
        self.assertFalse(hasattr(verified._manager,"session_store"))

    def switch_job(self, workflow, job):
        environment = BINDINGS[(workflow, job)]
        self.f.workflow, self.f.job = workflow, job
        self.f.claims_changed(workflow_ref=f"{REPO}/.github/workflows/{workflow}@refs/heads/main",
                              environment=environment,
                              sub=f"repo:cloud-computing-microservice@328679702/techx-infra@1322975868:environment:{environment}")
        self.f.run["path"] = f".github/workflows/{workflow}"
        self.f.listing["jobs"][0]["name"] = job
        self.manager = self.f.authenticate()

    def test_cleanup_arm_uses_start_job_and_dedicated_receiver(self):
        self.switch_job("staging-lifecycle.yml", "start")
        intent = dict(self.intent, key=self.intent["key"].replace("/deployment/", "/cleanup/"))
        packet = build_handoff(self.manager, self.f.token, operation="arm-cleanup", intent_reference=intent)
        receiver = ServerManagerHandoff(self.f.bridge, ReceiverSTS(CLEANUP), function_arn=CLEANUP)
        context = SimpleNamespace(invoked_function_arn=CLEANUP, aws_request_id="cleanup-invocation")
        result = receiver.verify(self.event(packet), context)
        self.assertEqual(result.as_dict()["request"]["operation"], "arm-cleanup")
        self.assertEqual(self.f.aws.puts(), [])

    def test_cleanup_request_remains_verifiable_after_window_closes(self):
        self.switch_job("staging-destroy.yml", "cleanup")
        self.f.aws.now += timedelta(hours=11)
        stamp = int(self.f.aws.now.timestamp())
        self.f.claims_changed(iat=stamp-15, nbf=stamp-20, exp=stamp+285)
        self.f.listing["jobs"][0]["started_at"] = self.f.aws.now.isoformat().replace("+00:00","Z")
        self.manager = self.f.authenticate()
        self.assertTrue(self.manager.as_dict()["observeOnly"])
        packet = build_handoff(self.manager, self.f.token, operation="request-cleanup")
        receiver = ServerManagerHandoff(self.f.bridge, ReceiverSTS(CLEANUP), function_arn=CLEANUP)
        result = receiver.verify(self.event(packet), SimpleNamespace(invoked_function_arn=CLEANUP, aws_request_id="cleanup-invocation"))
        self.assertEqual(result.as_dict()["request"]["operation"], "request-cleanup")

    def test_expired_handoff_token_is_rejected_before_remote_exchange(self):
        event = self.event(); before = len(self.f.exchanges)
        self.f.aws.now += timedelta(minutes=6)
        with self.assertRaises(AuthenticationFailed): self.receiver.verify(event, self.context)
        self.assertEqual(len(self.f.exchanges), before)

    def transport(self, *, failure=None):
        calls = []
        # Existing authenticated sender already owns this client; use a real
        # interface-shaped fake only for the transport, not identity verification.
        class Lambda:
            meta = SimpleNamespace(endpoint_url=f"https://lambda.{REGION}.amazonaws.com", region_name=REGION,
                                   config=SimpleNamespace(retries={"total_max_attempts":1}))
            def invoke(inner, **params):
                calls.append(params)
                if failure == "timeout": raise TimeoutError("secret-token-transport-message")
                verified = self.receiver.verify(json.loads(params["Payload"]), self.context)
                reply = observation_ack(verified)
                if failure == "fake-success": reply["status"] = "SUCCEEDED"
                response = dict(StatusCode=200, Payload=io.BytesIO(encoded(reply)), ResponseMetadata=dict(HTTPStatusCode=200,RequestId="invoke-request"))
                if failure == "function-error": response["FunctionError"] = "Unhandled"
                return response
        self.manager._clients["lambda"] = Lambda()
        return calls

    def test_sender_to_owned_receiver_is_sync_no_log_tail_no_client_context(self):
        calls = self.transport(); packet = self.packet()
        ack = invoke_handoff(self.manager, packet)
        self.assertEqual(ack["status"], "OBSERVATION_REQUIRED")
        self.assertEqual(calls[0]["FunctionName"], LIFECYCLE)
        self.assertEqual(calls[0]["InvocationType"], "RequestResponse")
        self.assertEqual(calls[0]["LogType"], "None")
        self.assertNotIn("ClientContext", calls[0])
        with self.assertRaises(HandoffRejected): invoke_handoff(self.manager,packet)
        self.assertEqual(len(calls),1)

    def test_transport_timeout_never_retries_or_leaks_raw_errors(self):
        calls = self.transport(failure="timeout"); packet = self.packet()
        with self.assertRaises(HandoffUncertain) as caught: invoke_handoff(self.manager,packet)
        self.assertNotIn("secret-token-transport-message",str(caught.exception))
        with self.assertRaises(HandoffRejected): invoke_handoff(self.manager,packet)
        self.assertEqual(len(calls),1)

    def test_function_error_or_fake_success_is_not_launch_success(self):
        for failure in ("function-error","fake-success"):
            calls = self.transport(failure=failure); packet = self.packet()
            with self.assertRaises(HandoffUncertain): invoke_handoff(self.manager,packet)
            self.assertEqual(len(calls),1)

    def test_actual_sdk_invoke_shape_and_sensitive_payload_metadata(self):
        import botocore.session
        from botocore.validate import validate_parameters
        calls = self.transport(); invoke_handoff(self.manager,self.packet())
        operation = botocore.session.get_session().get_service_model("lambda").operation_model("Invoke")
        validate_parameters(calls[0],operation.input_shape)
        self.assertTrue(operation.input_shape.members["Payload"].metadata.get("sensitive"))

    def test_other_manager_context_cannot_reuse_packet(self):
        packet = self.packet(); other = self.f.authenticate()
        with self.assertRaises(HandoffRejected): invoke_handoff(other,packet)

    def test_authenticated_receivers_are_separate_from_autonomous_internal_launchers(self):
        self.assertIn("manager-lifecycle",LIFECYCLE)
        self.assertIn("manager-cleanup-request",CLEANUP)
        self.assertNotEqual(LIFECYCLE,f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:techx-automation-v2-lifecycle-launcher")


if __name__ == "__main__": unittest.main()
