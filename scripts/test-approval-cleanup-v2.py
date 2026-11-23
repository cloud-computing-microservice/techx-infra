from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import hashlib
import json
import runpy
import unittest

from cleanup_v2 import INVENTORY_FIELDS, ValidatedFacts, compute_gone, read_provisional, validate_final, workload_zero
from execution_v2 import ACCOUNT, BUCKET, PREFIX
from plan_v2 import KMS_KEY, validate_apply, validate_request
from recovery_v2 import checksum


def pack(key, value, version="exact-version"):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return body, dict(bucket=BUCKET, key=key, versionId=version, sha256=hashlib.sha256(body).hexdigest())


def fixture():
    value = runpy.run_path(str(Path(__file__).with_name("test-execution-v2.py")))["V2Contracts"]()
    value.setUp()
    return value


class PlanContracts(unittest.TestCase):
    def setUp(self):
        self.f = f = fixture()
        self.worker = f.worker()
        self.now = f.now + timedelta(minutes=1)
        self.plan_bytes = b"synthetic saved plan"
        self.plan_ref = dict(bucket=BUCKET, key=f"{PREFIX}plans/{f.execution['sessionId']}/foundation/1/saved.tfplan",
                             versionId="plan-version", sha256=hashlib.sha256(self.plan_bytes).hexdigest())
        self.context = {k: deepcopy(f.execution[k]) for k in ("sourceRevisions", "approvedBudgetUsd", "cleanupStartAt", "deadline")}
        self.context.update(workerIdentitySha256=checksum(self.worker), configurationReference=deepcopy(self.worker["configurationReference"]),
                            backendBucket=BUCKET, backendKey="staging/terraform.tfstate", kmsKeyArn=KMS_KEY, terraformVersion="1.15.5",
                            configSha256="1" * 64, providerLockSha256="2" * 64,
                            stateReference=dict(bucket=BUCKET, key="staging/terraform.tfstate", versionId="state-version", sha256="3" * 64),
                            stateLineage="12345678-1234-1234-1234-123456789abc", stateSerial=42)
        self.request = dict(schemaVersion=2, sessionId=f.execution["sessionId"], executionReference=deepcopy(f.ref), leaseEpoch=1,
                            stage="foundation", attempt=1, planReference=deepcopy(self.plan_ref), context=deepcopy(self.context),
                            createdAt="2026-11-22T01:00:00Z", expiresAt="2026-11-22T02:00:00Z")
        self.request_body, self.request_ref = self.request_pack(self.request)
        self.review = dict(repositoryId="1322975868", ownerId="328679702", workflow="staging-plan-review.yml", runId="234567",
                           runAttempt=1, headSha=f.execution["sourceRevisions"]["techx-infra"], environment="staging-apply",
                           environmentId=1234, reviewerId=5678, reviewerLogin="human-reviewer", reviewerType="User",
                           runCreatedAt="2026-11-22T01:00:10Z", verifiedAt="2026-11-22T01:00:20Z")
        self.approval = dict(schemaVersion=2, requestReference=deepcopy(self.request_ref), review=deepcopy(self.review),
                             approvedAt="2026-11-22T01:00:21Z", expiresAt="2026-11-22T02:00:00Z")

    def request_pack(self, request):
        return pack(f"{PREFIX}reviews/{self.f.execution['sessionId']}/foundation/1/request.json", request)

    def check_request(self, request=None, context=None, worker=None, session=None):
        body, ref = self.request_pack(request if request is not None else self.request)
        return validate_request(body, ref, self.f.execution, self.f.ref, session if session is not None else self.f.session,
                                worker if worker is not None else self.worker, context if context is not None else self.context, now=self.now)

    def apply(self, approval=None, downloaded=None, plan_bytes=None, review=None):
        body, ref = pack(f"{PREFIX}approvals/{self.f.execution['sessionId']}/{self.request_ref['sha256']}.json",
                         approval if approval is not None else self.approval)
        return validate_apply(self.request_body, self.request_ref, body, ref,
                              plan_bytes if plan_bytes is not None else self.plan_bytes,
                              downloaded if downloaded is not None else self.plan_ref,
                              self.f.execution, self.f.ref, self.f.session, self.worker, self.context,
                              review if review is not None else self.review, now=self.now)

    def test_exact_private_plan_and_independent_human_review(self):
        self.assertEqual(self.check_request(), self.request)
        self.assertEqual(self.apply()["planReference"], self.plan_ref)
        self.assertEqual(self.apply(review=dict(self.review, verifiedAt="2026-11-22T01:00:30Z"))["leaseEpoch"], 1)

    def test_request_cannot_change_scope_epoch_stage_or_window(self):
        for key, value in [("schemaVersion", True), ("sessionId", "staging-v2-other"), ("leaseEpoch", True), ("leaseEpoch", 2),
                           ("stage", "edge"), ("attempt", 2), ("createdAt", "2026-11-22T01:01:01Z"),
                           ("expiresAt", "2026-11-22T01:01:00Z"), ("expiresAt", "2026-11-22T09:00:01Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.check_request(dict(self.request, **{key: value}))
        for status in ("DESTROYING", "READY", "FAILED_NEEDS_CLEANUP"):
            with self.subTest(status=status), self.assertRaises(ValueError): self.check_request(session=dict(self.f.session, status=status))

    def test_fresh_context_change_requires_replan(self):
        for key, value in [("stateSerial", 43), ("stateSerial", True), ("stateLineage", "abcdefab-1234-1234-1234-123456789abc"),
                           ("configSha256", "f" * 64), ("providerLockSha256", "f" * 64), ("terraformVersion", "1.15.4"),
                           ("backendBucket", "another-bucket"), ("kmsKeyArn", "another-key"), ("approvedBudgetUsd", "59"),
                           ("stateReference", dict(self.context["stateReference"], versionId="new-state-version")),
                           ("configurationReference", dict(self.context["configurationReference"], versionId="new-config"))]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.check_request(context=dict(self.context, **{key: value}))

    def test_same_instance_with_new_boot_or_ip_requires_replan(self):
        for changes in [dict(PublicIpAddress="8.8.4.4"), dict(LaunchTime=self.f.now + timedelta(seconds=1))]:
            current = self.f.worker(instance=dict(self.f.instance, **changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.check_request(worker=current, context=dict(self.context, workerIdentitySha256=checksum(current)))

    def test_identical_plan_bytes_from_different_s3_version_are_rejected(self):
        for changes in [dict(versionId="other-version"), dict(versionId="null"), dict(bucket="other-bucket"), dict(sha256="f" * 64)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.apply(downloaded=dict(self.plan_ref, **changes))
        for body in (b"", b"changed saved plan"):
            with self.subTest(body=body), self.assertRaises(ValueError): self.apply(plan_bytes=body)

    def test_approval_cannot_rebind_request_or_reviewer(self):
        for key, value in [("schemaVersion", True), ("requestReference", dict(self.request_ref, versionId="other")),
                           ("approvedAt", "2026-11-22T01:00:19Z"), ("expiresAt", "2026-11-22T02:00:01Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.apply(dict(self.approval, **{key: value}))
        for key, value in [("reviewerId", 9999), ("reviewerType", "Bot"), ("reviewerId", True), ("repositoryId", "1234"),
                           ("runAttempt", 2), ("environment", "staging-start"), ("workflow", "verify.yml"),
                           ("verifiedAt", "2026-11-22T01:01:01Z"), ("runCreatedAt", "2026-11-22T00:59:59Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.apply(review=dict(self.review, **{key: value}))


class CleanupContracts(unittest.TestCase):
    def setUp(self):
        self.f = f = fixture()
        self.now, self.after = f.now + timedelta(minutes=2), f.now
        self.epoch = 2
        self.request_no = 0
        self.cleanup_id = "i-" + "2" * 17
        self.registry = dict(schemaVersion=2, executionReference=deepcopy(f.ref), leaseEpoch=2, runtimeRevision=4,lastMutationId="a"*32,launches=[])
        for index, purpose, instance_id in [(1, "deployment", f.instance["InstanceId"]), (2, "cleanup", self.cleanup_id)]:
            self.registry["launches"].append(dict(purpose=purpose, attempt=1, phase="OBSERVED", launchToken=str(index) * 64,
                                                launchEpoch=1,
                                                intentReference=dict(bucket=BUCKET,key=f"{PREFIX}launches/{f.execution['sessionId']}/{purpose}/1/intent.json",versionId="intent-version",sha256="a"*64),
                                                checkpoint=dict(schemaVersion=2,launchToken=str(index)*64,phase="OBSERVED",callStartedAt="2026-11-22T01:00:00Z",
                                                                instanceId=instance_id,observedAt="2026-11-22T01:00:10Z",workerIdentitySha256="b"*64),
                                                registrationReference=dict(bucket=BUCKET,key=f"{PREFIX}workers/{f.execution['sessionId']}/registrations/AROA{'A'*17}:{instance_id}.json",versionId="registration-version",sha256="c"*64),
                                                instanceId=instance_id, volumeIds=["vol-" + str(index) * 17], networkInterfaceIds=["eni-" + str(index) * 17], terminalReference=None))
        self.observations = dict(schemaVersion=2, executionReference=deepcopy(f.ref), leaseEpoch=2, observedAt="2026-11-22T01:01:00Z",
                                 discoveredInstanceIds=[f.instance["InstanceId"], self.cleanup_id], instances={}, volumes={}, networkInterfaces={}, terminalProofs={})
        for row in self.registry["launches"]:
            tags = dict(Project="TechX", Environment="automation", SessionId=f.execution["sessionId"],
                        ExecutionSha256=f.ref["sha256"], Role=f"{row['purpose']}-executor")
            instance = dict(InstanceId=row["instanceId"], ClientToken=row["launchToken"], State=dict(Name="terminated"),
                            Tags=[dict(Key=k, Value=v) for k, v in tags.items()], NetworkInterfaces=[], BlockDeviceMappings=[])
            self.observations["instances"][row["instanceId"]] = self.fact(dict(Reservations=[dict(Instances=[instance])]), row["instanceId"], "DescribeInstances")
            self.observations["volumes"][row["volumeIds"][0]] = self.not_found("InvalidVolume.NotFound", row["volumeIds"][0], "DescribeVolumes")
            self.observations["networkInterfaces"][row["networkInterfaceIds"][0]] = self.not_found("InvalidNetworkInterfaceID.NotFound", row["networkInterfaceIds"][0], "DescribeNetworkInterfaces")
        self.state = dict(version=4, serial=42, lineage="12345678-1234-1234-1234-123456789abc", resources=[])
        self.state_body, self.state_ref = pack("staging/terraform.tfstate", self.state)
        self.inventory = dict(schemaVersion=2, executionReference=deepcopy(f.ref), leaseEpoch=2, snapshotId="a" * 32,
                              observedAt="2026-11-22T01:01:00Z", stateReference=deepcopy(self.state_ref),
                              inventory=dict(accountId=ACCOUNT, region="us-east-1", environment="staging", **{k: [] for k in INVENTORY_FIELDS}))
        self.provisional = dict(schemaVersion=2, executionReference=deepcopy(f.ref), leaseEpoch=2, cleanupInstanceId=self.cleanup_id,
                                snapshotId="b" * 32, inventoryReference=dict(bucket=BUCKET, key=f"{PREFIX}cleanup/{f.execution['sessionId']}/inventories/{'b'*32}.json", versionId="prior-inventory", sha256="b" * 64),
                                stateReference=deepcopy(self.state_ref), computeReference=dict(bucket=BUCKET,
                                    key=f"{PREFIX}cleanup/{f.execution['sessionId']}/provisional-compute/2.json", versionId="prior-compute", sha256="c" * 64),
                                createdAt="2026-11-22T01:00:30Z")
        self.provisional_body, self.provisional_ref = pack(f"{PREFIX}cleanup/{f.execution['sessionId']}/provisional/2.json", self.provisional)
        self.caller = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-finalizer/techx-automation-v2-finalizer")

    def fact(self, response, resource_id="unknown-id", operation="unknown-api", status=200):
        self.request_no += 1
        return dict(observedAt="2026-11-22T01:01:00Z", requestedId=resource_id, operation=operation,
                    response=dict(response, ResponseMetadata=dict(HTTPStatusCode=status, RequestId=f"request-{self.request_no:08d}")))

    def not_found(self, code, resource_id="unknown-id", operation="unknown-api"):
        return self.fact(dict(Error=dict(Code=code)), resource_id, operation, status=400)

    def workload(self, inventory=None, state=None):
        value = inventory if inventory is not None else self.inventory
        state_body, state_ref = pack("staging/terraform.tfstate", state if state is not None else self.state)
        body, ref = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/inventories/{value['snapshotId']}.json", value)
        return workload_zero(body, ref, state_body, state_ref, self.f.execution, self.f.ref, self.epoch, now=self.now, after=self.after)

    def compute(self, registry=None, observations=None, final=True):
        registry = registry if registry is not None else self.registry
        _, ref = pack(f"{PREFIX}workers/{self.f.execution['sessionId']}/registry.json", registry)
        observations = observations if observations is not None else self.observations
        kind = "compute-observations" if final else "provisional-compute-observations"
        _, observation_ref = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/{kind}/2.json", observations)
        return compute_gone(registry, ref, observations, observation_ref, self.f.execution, self.f.ref,
                            self.epoch, active_cleanup_id=self.cleanup_id, final=final, now=self.now, after=self.after)

    def finish(self, workload=None, compute=None, changes=None, caller=None):
        workload = workload if workload is not None else self.workload()
        compute = compute if compute is not None else self.compute()
        wd = workload.as_dict() if type(workload) is ValidatedFacts else workload
        cd = compute.as_dict() if type(compute) is ValidatedFacts else compute
        _, wr = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/workload/2.json", wd)
        _, cr = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/compute/2.json", cd)
        value = dict(schemaVersion=2, executionReference=deepcopy(self.f.ref), leaseEpoch=2,
                      provisionalReference=deepcopy(self.provisional_ref), inventoryReference=wd["inventoryReference"], stateReference=wd["stateReference"],
                      workloadReference=wr, computeReference=cr, completedAt="2026-11-22T01:01:01Z", finalizerPrincipalArn=self.caller["Arn"])
        value.update(changes or {})
        body, ref = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/final/2.json", value)
        return validate_final(body, ref, self.provisional_body, self.provisional_ref, self.f.execution, self.f.ref,
                              dict(self.f.session, status="DESTROYING", leaseEpoch=2), workload, compute,
                              wr, cr,
                              caller=caller if caller is not None else self.caller, now=self.now)

    def test_workload_state_and_all_inventory_groups_are_independent(self):
        self.assertEqual(self.workload().as_dict()["stateSerial"], 42)
        for name in INVENTORY_FIELDS:
            for bad in (["remaining-resource"], None):
                value = deepcopy(self.inventory); value["inventory"][name] = bad
                with self.subTest(group=name, bad=bad), self.assertRaises(ValueError): self.workload(value)
        for state in [dict(self.state, resources=[dict(mode="managed", instances=[{}])]),
                      dict(self.state, resources=None), dict(self.state, serial=True), dict(self.state, version=True)]:
            # Update the inventory's state reference too, so this tests state
            # content rather than merely a mismatched checksum.
            _, sr = pack("staging/terraform.tfstate", state)
            with self.subTest(state=state), self.assertRaises(ValueError): self.workload(dict(self.inventory, stateReference=sr), state)

    def test_snapshot_scope_fence_and_freshness_cannot_be_forged(self):
        for key, value in [("schemaVersion", True), ("leaseEpoch", True), ("leaseEpoch", 1),
                           ("observedAt", "2026-11-22T00:59:59Z"), ("observedAt", "2026-11-22T01:02:01Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.workload(dict(self.inventory, **{key: value}))
        changed = deepcopy(self.inventory); changed["inventory"]["accountId"] = "111111111111"
        with self.assertRaises(ValueError): self.workload(changed)

    def test_all_workers_and_assets_gone_before_final_seal(self):
        facts = self.compute().as_dict()
        self.assertEqual(facts["phase"], "ALL_SESSION_COMPUTE_GONE")
        self.assertEqual(len(facts["instanceIds"]), 2)
        self.assertEqual(self.finish()["leaseEpoch"], 2)

    def test_calling_missing_roster_and_unknown_discovery_block_finalization(self):
        for registry in [dict(self.registry, launches=[]), dict(self.registry, leaseEpoch=1)]:
            with self.subTest(registry=registry), self.assertRaises(ValueError): self.compute(registry=registry)
        bare = {k:v for k,v in self.registry.items() if k not in {"runtimeRevision","lastMutationId"}}
        with self.assertRaises(ValueError): self.compute(registry=bare)
        for phase in ("CALLING", "OFF"):
            registry = deepcopy(self.registry); registry["launches"][0]["phase"] = phase
            with self.subTest(phase=phase), self.assertRaises(ValueError): self.compute(registry=registry)
        for changes in [dict(discoveredInstanceIds=["i-" + "f" * 17]), dict(instances={}), dict(volumes={}), dict(networkInterfaces={})]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.compute(observations=dict(self.observations, **changes))

    def test_running_stopped_or_unknown_instance_is_not_terminal(self):
        for state in ("running", "stopped", "stopping", "shutting-down", "pending"):
            observations = deepcopy(self.observations)
            observations["instances"][self.cleanup_id]["response"]["Reservations"][0]["Instances"][0]["State"]["Name"] = state
            with self.subTest(state=state), self.assertRaises(ValueError): self.compute(observations=observations)
        for code in ("UnauthorizedOperation", "RequestLimitExceeded"):
            observations = deepcopy(self.observations); observations["instances"][self.cleanup_id] = self.not_found(code, self.cleanup_id, "DescribeInstances")
            with self.subTest(code=code), self.assertRaises(ValueError): self.compute(observations=observations)

    def test_absence_after_registered_instance_record_expires_is_positive(self):
        observations = deepcopy(self.observations)
        observations["instances"][self.cleanup_id] = self.not_found("InvalidInstanceID.NotFound", self.cleanup_id, "DescribeInstances")
        with self.assertRaises(ValueError): self.compute(observations=observations)
        registry = deepcopy(self.registry)
        proof = dict(schemaVersion=2, executionReference=deepcopy(self.f.ref), instanceId=self.cleanup_id,
                     observation=deepcopy(self.observations["instances"][self.cleanup_id]))
        proof["observation"]["observedAt"] = "2026-11-22T01:00:45Z"
        body, ref = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/terminals/{self.cleanup_id}.json", proof)
        registry["launches"][1]["terminalReference"] = ref
        observations["terminalProofs"][self.cleanup_id] = dict(reference=ref, body=body.decode())
        self.assertEqual(self.compute(registry=registry, observations=observations).as_dict()["phase"], "ALL_SESSION_COMPUTE_GONE")
        proof["observation"]["response"]["Reservations"][0]["Instances"][0]["State"]["Name"] = "running"
        body, ref = pack(ref["key"], proof)
        registry["launches"][1]["terminalReference"] = ref
        observations["terminalProofs"][self.cleanup_id] = dict(reference=ref, body=body.decode())
        with self.assertRaises(ValueError): self.compute(registry=registry, observations=observations)

    def test_api_errors_stale_facts_and_surviving_assets_never_become_empty(self):
        for group in ("volumes", "networkInterfaces"):
            for response in [self.fact({}), self.not_found("AccessDenied"), self.not_found("RequestLimitExceeded")]:
                observations = deepcopy(self.observations); resource_id = next(iter(observations[group]))
                response["requestedId"] = resource_id
                response["operation"] = "DescribeVolumes" if group == "volumes" else "DescribeNetworkInterfaces"
                observations[group][resource_id] = response
                with self.subTest(group=group, response=response), self.assertRaises(ValueError): self.compute(observations=observations)
        observations = deepcopy(self.observations)
        observations["instances"][self.cleanup_id]["observedAt"] = "2026-11-22T00:59:59Z"
        with self.assertRaises(ValueError): self.compute(observations=observations)

    def test_terminated_instance_cannot_keep_nic_volume_or_public_address(self):
        for key, value in [("PublicIpAddress", "8.8.8.8"), ("NetworkInterfaces", [{}]), ("BlockDeviceMappings", [{}])]:
            observations = deepcopy(self.observations)
            observations["instances"][self.cleanup_id]["response"]["Reservations"][0]["Instances"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.compute(observations=observations)

    def provisional_compute(self):
        observations = deepcopy(self.observations)
        observations["instances"][self.cleanup_id]["response"]["Reservations"][0]["Instances"][0]["State"]["Name"] = "running"
        del observations["volumes"]["vol-" + "2" * 17]
        del observations["networkInterfaces"]["eni-" + "2" * 17]
        return self.compute(observations=observations, final=False)

    def test_provisional_receipt_never_substitutes_for_cleanup_owner_termination(self):
        self.assertEqual(self.provisional_compute().as_dict()["phase"], "INFRA_STOPPED_CLEANUP_OWNER_LIVE")
        with self.assertRaises(ValueError): self.finish(compute=self.provisional_compute())
        self.assertEqual(read_provisional(self.provisional_body, self.provisional_ref, self.f.execution, self.f.ref, 2), self.provisional)

    def test_only_real_finalizer_context_and_validated_in_process_facts_can_seal(self):
        for caller in [self.f.worker_caller, self.f.manager_caller, dict(self.caller, Account="111111111111")]:
            with self.subTest(caller=caller), self.assertRaises(ValueError): self.finish(caller=caller)
        with self.assertRaises(ValueError): self.finish(compute=self.compute().as_dict())
        with self.assertRaises(ValueError): self.finish(workload=self.workload().as_dict())
        with self.assertRaises(ValueError): ValidatedFacts("compute", self.compute().as_dict())
        facts = self.compute(); exposed = facts.as_dict(); exposed["phase"] = "forged"
        self.assertEqual(facts.as_dict()["phase"], "ALL_SESSION_COMPUTE_GONE")

    def test_final_receipt_cannot_rebind_epoch_artifacts_or_completion_time(self):
        _, changed_ref = pack(f"{PREFIX}cleanup/{self.f.execution['sessionId']}/compute/2.json", self.compute().as_dict(), version="different-version")
        for changes in [dict(leaseEpoch=True), dict(leaseEpoch=1), dict(completedAt="2026-11-22T01:00:59Z"),
                        dict(completedAt="2026-11-22T01:02:01Z"), dict(finalizerPrincipalArn=self.f.worker_caller["Arn"]),
                        dict(computeReference=changed_ref),
                        dict(provisionalReference=dict(self.provisional_ref, versionId="other-version"))]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.finish(changes=changes)

    def test_copying_one_absence_response_across_ids_is_not_evidence(self):
        observations = deepcopy(self.observations)
        ids = list(observations["volumes"])
        observations["volumes"][ids[1]] = deepcopy(observations["volumes"][ids[0]])
        with self.assertRaises(ValueError): self.compute(observations=observations)
        # Even relabeling the requested ID cannot reuse one singleton API request.
        observations["volumes"][ids[1]]["requestedId"] = ids[1]
        with self.assertRaises(ValueError): self.compute(observations=observations)

    def test_evidence_older_than_five_minutes_cannot_be_reused(self):
        self.now += timedelta(minutes=8)
        with self.assertRaises(ValueError): self.compute()
        with self.assertRaises(ValueError): self.workload()


if __name__ == "__main__":
    unittest.main()
