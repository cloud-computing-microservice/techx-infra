"""Actual protected rollback review/index and single-CAS grant consumption."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from rollback_grant import consume_grant, reviewed_grant
from rollback_task_contract import proposal_record, rollout_context

spec = importlib.util.spec_from_file_location("rollback_grant_review_fixture", Path(__file__).with_name("test-rollback-review.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ProtectedReviewTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        self.f, self.d, self.infra = self.p.f, self.p.d, self.p.infra
        self.request = self.p.preview()
        self.reference = self.p.approve(self.request)
        self.p.run.update(status="completed", conclusion="success", updated_at=self.f.now.isoformat())
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"

    def consume(self):
        return consume_grant(self.d, self.infra, self.p.p.chart.github, self.p.p.p.runtime.source, "300")

    def test_exact_successful_review_is_consumed_once_without_merge_ready_or_deadline_extension(self):
        before = deepcopy(self.f.record)
        self.assertEqual(self.consume(), self.reference)
        self.assertEqual(self.consume(), self.reference)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], before["acceptedRelease"])
        self.assertEqual(self.f.record["rollbackAttempt"]["intent"], before["rollbackAttempt"]["intent"])
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "APPROVED")
        self.assertEqual(rollout_context(self.f.record)["approvalReference"], self.reference)
        self.assertEqual(proposal_record(self.f.record)["rollbackAttempt"], before["rollbackAttempt"])
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertFalse(self.p.p.chart.pr["merged"])

    def test_lost_successful_cas_is_read_back_without_reissuing_a_grant(self):
        write = self.f.store.write
        def lost(record, etag): write(record, etag); raise TimeoutError("Consumed; response lost")
        with patch.object(self.f.store, "write", side_effect=lost), self.assertRaises(TimeoutError): self.consume()
        with patch.object(self.f.store, "write", side_effect=AssertionError("No second CAS")):
            self.assertEqual(self.consume(), self.reference)

    def test_failed_running_or_wrong_actual_review_cannot_consume_the_grant(self):
        for fields in (dict(status="in_progress"), dict(conclusion="failure"), dict(run_attempt=2), dict(display_title="other")):
            original = deepcopy(self.p.run); self.p.run.update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.consume()
            self.p.run = original
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "PROPOSED")

    def test_revoked_environment_or_pr_review_and_new_failed_ci_block_consumption(self):
        self.p.approvals.clear()
        with self.assertRaises(ValueError): self.consume()
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "PROPOSED")

    def test_cleanup_race_cannot_be_replaced_by_an_approved_slot(self):
        write = self.f.store.write
        def race(record, etag):
            current, tag = self.f.store.read(); current["status"] = "DESTROYING"; write(current, tag)
            return write(record, etag)
        with patch.object(self.f.store, "write", side_effect=race), self.assertRaises(Exception): self.consume()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "PROPOSED")

    def test_expired_grant_and_changed_index_do_not_change_the_slot(self):
        self.f.now += timedelta(minutes=16)
        with self.assertRaises(ValueError): self.consume()
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "PROPOSED")


if __name__ == "__main__": unittest.main()
