"""Private single-use rollback intent and exact baseline-restoration proposal."""
from copy import deepcopy
from types import SimpleNamespace

from automation_budget import validate_estimate
from automation_session import claim, utc
from chart_promotion import protection
from cleanup_completion import CONTEXT
from cleanup_schedule import verify_existing
from deployment_tasks import read_task
from plan_review_store import checksum, immutable_json
from private_active_rollout import active_clients
from promotion_authority import admitted
from rollback_admission import preview_public_rollback
from rollback_proposal import BaselineRevert
from rollback_task_contract import context, key, original_record, read_proposal_receipt, slot


class PrivateRollbackPrepare:
    def __init__(self, runtime, store, execution_version, task_id, *, clock, clients=active_clients):
        self.runtime, self.store, self.version, self.task_id = runtime, store, execution_version, task_id
        self.clock, self.client_factory = clock, clients

    def base(self):
        record, etag = self.store.read()
        value = slot(record)
        lease = record.get("lease", {})
        if (record.get("executionVersion") != self.version or any(record.get(name) != self.runtime.execution.get(name) for name in CONTEXT)
                or lease.get("owner") != self.owner or lease.get("epoch") != self.epoch or lease.get("operation") != "rollback"
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Rollback preparation lost its owned immutable execution/lease")
        claim(record, self.owner, "rollback", now=self.clock())
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        instance = self.runtime.private["deploymentExecutor"]["instanceId"]
        if ((task["mode"], task["stage"], task["attempt"]) != ("rollback", "prepare", 1)
                or task["instanceId"] != instance or self.owner != f"arn:aws:ec2:us-east-1:058114477594:instance/{instance}"
                or record["deploymentTask"].get("state") != "RUNNING" or task["expectedRollbackContext"] != context(record)):
            raise ValueError("Rollback preparation is not this executor's exact claimed task")
        return record, etag

    def guard(self):
        record, _ = self.base()
        validate_estimate(record, self.runtime.execution["costEstimate"])
        verify_existing(self.runtime.clients["scheduler"], record, self.version, self.clock())
        def original():
            current, etag = self.base()
            return original_record(current), etag
        intent = preview_public_rollback(self.runtime.clients, self.runtime.bucket, SimpleNamespace(read=original),
            self.runtime.execution, self.version, self.infra, clock=self.clock, created_at=slot(record)["intent"]["createdAt"])
        if intent != slot(record)["intent"]: raise ValueError("Private rollback predecessor differs from the reserved intent")
        policy = protection(self.chart, self.policy["appId"], self.policy["appSlug"])
        if policy != {name: self.policy[name] for name in ("appId", "appSlug", "policySha256")}:
            raise ValueError("Rollback App/branch policy differs from the opening approval")
        return intent

    def checkpoint(self, **fields):
        self.guard()
        record, etag = self.base()
        updated = deepcopy(record)
        updated["rollbackAttempt"].update(fields)
        slot(updated)
        self.store.write(updated, etag)

    def perform(self, owner, epoch):
        self.owner, self.epoch = owner, epoch
        record, _ = self.base()
        if slot(record)["phase"] != "REQUESTED": raise ValueError("Rollback preparation cannot replay a consumed task")
        self.policy = admitted(self.runtime.execution)
        self.chart, self.infra, _ = self.client_factory(self.runtime, self.policy, self.clock, self.version)
        intent = self.guard()
        intent_ref = immutable_json(self.runtime.clients["s3"], self.runtime.bucket, key(record, "intent"), intent)
        intent_ref["sha256"] = checksum(intent)
        self.checkpoint(phase="PREPARING", intentReference=intent_ref)
        self.producer = BaselineRevert(self.chart, intent, self.guard, clock=self.clock)
        proposal = self.producer.perform(self.runtime.source)
        self.guard()
        record, _ = self.base()
        receipt = dict(schemaVersion=1, kind="private-rollback-proposal", sessionId=record["sessionId"], executionVersion=self.version,
            taskId=self.task_id, rollbackContext=context(record), intentReference=intent_ref, proposal=proposal)
        ref = immutable_json(self.runtime.clients["s3"], self.runtime.bucket, key(record, "proposal"), receipt)
        ref["sha256"] = checksum(receipt)
        self.guard()
        proposed = deepcopy(self.base()[0])
        proposed["rollbackAttempt"].update(phase="PROPOSED", proposalReference=ref, prepareTaskId=self.task_id)
        read_proposal_receipt(self.runtime.clients["s3"], self.runtime.bucket, proposed, self.version)
        self.checkpoint(phase="PROPOSED", proposalReference=ref, prepareTaskId=self.task_id)
        return dict(state="ROLLBACK_PROPOSED", proposalReference=ref, pullRequest=proposal["pullRequest"])
