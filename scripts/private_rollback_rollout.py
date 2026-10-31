"""One approved rollback merge and pinned Application restore; never READY."""
from copy import deepcopy
from datetime import timedelta
import time
from types import SimpleNamespace

from automation_session import utc
from plan_review_store import checksum
from private_active_rollout import PrivateActiveRollout, STAMP
from private_chart_promotion import app_clients
from rollback_chart_promotion import RollbackPromotionChecks
from rollback_grant import reviewed_grant
from rollback_proposal import baseline_restore_tree
from rollback_rollout_contract import failed_rollout, read_rollback_rollout
from rollback_task_contract import key, rollout_context, slot


class PrivateRollbackRollout(PrivateActiveRollout):
    def __init__(self, runtime, store, execution_version, task_id, *, clock, sleep=time.sleep, clients=app_clients):
        self.runtime, self.store, self.version, self.task_id = runtime, store, execution_version, task_id
        self.clock, self.sleep, self.client_factory = clock, sleep, clients
        self.checks = RollbackPromotionChecks(runtime.clients, store, runtime.bucket, runtime.execution, execution_version,
            infra=None, rollout_task=task_id, clock=clock)
        self.control = SimpleNamespace(owned=self.owned)

    def owned(self, session, owner, epoch):
        if session != self.runtime.execution["sessionId"]: raise ValueError("Rollback ownership belongs to another session")
        return self.checks.base(owner, epoch)

    def clients(self, runtime, policy, clock, execution_version):
        chart, infra = self.client_factory(runtime, policy, clock, execution_version)
        self.checks.infra = infra
        return chart, infra

    def approval_reference(self, record): return slot(record)["approvalReference"]

    def perform(self, owner, epoch):
        self.owner, self.epoch = owner, epoch
        if slot(self.guard())["phase"] != "APPROVED": raise ValueError("Rollback rollout cannot replay its restored task")
        return super().perform(owner, epoch)

    def guard(self):
        record, _ = self.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        context = rollout_context(record)
        instance = self.runtime.private["deploymentExecutor"]["instanceId"]
        if (self.owner != f"arn:aws:ec2:us-east-1:058114477594:instance/{instance}"
                or record["deploymentTask"].get("instanceId") != instance
                or hasattr(self, "context") and context != self.context
                or hasattr(self, "approval_ref") and self.approval_reference(record) != self.approval_ref):
            raise ValueError("Rollback rollout lost its same assigned executor, task or grant")
        if hasattr(self, "infra"):
            self.checks.guard(owner=self.owner, epoch=self.epoch)
            reviewed_grant(self.runtime.clients, self.runtime.bucket, record, self.runtime.execution, self.version,
                self.infra, self.approval_reference(record), self.clock())
        return record

    def execution_end(self, record):
        return min(utc(slot(record)["intent"]["expiresAt"]), utc(record["cleanupStartAt"])) - timedelta(seconds=60)

    def unready_application(self, *args):
        raise TimeoutError("Rollback did not reach readiness in its original window; cleanup required")

    def application_context(self, record): return rollout_context(record)

    def application_key(self, record): return f"private/staging/{record['sessionId']}/rollback/application/intent.json"

    def application_previous_revision(self, record): return slot(record)["intent"]["failedChartSha"]

    def application_intent(self, record, receipt, merge_ref, uid, previous_spec):
        return dict(schemaVersion=1, kind="rollback-application-intent", sessionId=record["sessionId"], executionVersion=self.version,
            taskId=self.task_id, rollbackContext=self.context, mergeReceiptReference=merge_ref, chartSha=receipt["chartSha"],
            uid=uid, previousSpec=previous_spec)

    def application_predecessor(self, app, record, previous_spec):
        old = failed_rollout(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)
        if (app["metadata"]["uid"] != old["application"]["uid"] or checksum(previous_spec) != old["application"]["specSha256"]
                or app["metadata"].get("annotations", {}).get(STAMP) != old["application"]["stamp"]):
            raise ValueError("Rollback Application is not the exact previously observed failed release")

    def application_manifest(self, record):
        return baseline_restore_tree(self.chart, slot(record)["intent"], self.runtime.source)[-1]

    def application_patch_tests(self, record):
        old = failed_rollout(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)
        return [dict(op="test", path="/metadata/annotations/techx.io~1release-intent", value=old["application"]["stamp"])]

    def checkpoint(self, receipt, ref, request):
        chosen = receipt["chartSha"]
        ci = self.main_ci(chosen)
        application, images = self.application(receipt, ref, ci)
        ci = self.main_ci(chosen)
        current = self.guard()
        value = dict(schemaVersion=1, kind="rollback-rollout", sessionId=current["sessionId"], executionVersion=self.version,
            rollbackContext=rollout_context(current), taskId=self.task_id, chartSha=chosen, mergeReceiptReference=ref,
            chartCi=ci, application=application, images=images, completedAt=self.clock().isoformat())
        result = self.put(key(current, "rollout"), value)
        current, etag = self.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.guard()
        updated = deepcopy(current)
        updated["rollbackAttempt"].update(phase="RESTORED", rolloutTaskId=self.task_id, rolloutReference=result)
        read_rollback_rollout(self.runtime.clients["s3"], self.runtime.bucket, updated, self.version)
        self.store.write(updated, etag)
        return dict(state="ROLLBACK_RESTORED", chartSha=chosen, receiptReference=result)
