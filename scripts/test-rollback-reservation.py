"""Single-use rollback reservation and positive lost-CAS recovery, no dispatch."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from chart_selection import INFRA_REPO
from plan_review_store import encoded
from rollback_reservation import arm_public_rollback

spec = importlib.util.spec_from_file_location("rollback_reservation_fixture", Path(__file__).with_name("test-rollback-admission.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.a = support.AdmissionTests()
        self.a.setUp(); self.addCleanup(self.a.doCleanups)
        self.f, self.d = self.a.f, self.a.d
        self.now = self.f.now
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.own = dict(id=300, status="in_progress", head_branch="main", head_sha=self.a.execution["sourceRevisions"]["techx-infra"],
            path=".github/workflows/staging-orchestrate.yml", event="workflow_dispatch", run_attempt=1,
            created_at=self.now.isoformat(),
            repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        get = self.a.p.r.h.infra.get
        self.a.p.r.h.infra.get = lambda endpoint: deepcopy(self.own) if endpoint == "actions/runs/300" else get(endpoint)

    def arm(self):
        return arm_public_rollback(self.f.clients, self.d.bucket, self.f.store, self.a.execution, self.d.version,
            self.a.p.r.h.infra, "300", clock=lambda: self.now)

    def test_one_cas_reservation_retains_failed_state_and_accepted_baseline(self):
        original = deepcopy(self.f.record)
        writes, write = [], self.f.store.write
        def capture(record, etag): writes.append(deepcopy(record)); return write(record, etag)
        with patch.object(self.f.store, "write", side_effect=capture):
            slot = self.arm()
            self.now += timedelta(minutes=3)
            self.assertEqual(self.arm(), slot)
        self.assertEqual(len(writes), 1)
        self.assertEqual(slot["phase"], "REQUESTED")
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], original["acceptedRelease"])
        self.assertEqual(self.f.record["sourceRevisions"], original["sourceRevisions"])
        self.assertNotIn("lease", self.f.record)

    def test_lost_successful_cas_is_recovered_without_new_attempt_or_deadline(self):
        write = self.f.store.write
        def lost(record, etag): write(record, etag); raise TimeoutError("CAS stored; response lost")
        with patch.object(self.f.store, "write", side_effect=lost), self.assertRaises(TimeoutError): self.arm()
        first = deepcopy(self.f.record["rollbackAttempt"])
        self.now += timedelta(minutes=1)
        with patch.object(self.f.store, "write", side_effect=AssertionError("Cannot repeat reservation")):
            self.assertEqual(self.arm(), first)

    def test_used_or_expired_slot_never_becomes_a_fresh_attempt(self):
        slot = self.arm()
        self.f.record["rollbackAttempt"]["phase"] = "COMPLETE"
        with self.assertRaises(ValueError): self.arm()
        self.f.record["rollbackAttempt"] = deepcopy(slot)
        self.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(self.f.record["rollbackAttempt"], slot)

    def test_public_runner_or_wrong_controller_cannot_reserve_an_attempt(self):
        self.f.identity["Arn"] = self.a.p.worker.owner
        with self.assertRaises(ValueError): self.arm()
        self.assertNotIn("rollbackAttempt", self.f.record)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.own["head_sha"] = "0" * 40
        with self.assertRaises(ValueError): self.arm()

    def test_cleanup_wins_a_cas_race_without_losing_its_state(self):
        write = self.f.store.write
        def race(record, etag):
            current, current_etag = self.f.store.read(); current["status"] = "DESTROYING"
            write(current, current_etag)
            return write(record, etag)
        with patch.object(self.f.store, "write", side_effect=race), self.assertRaises(Exception): self.arm()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_new_failure_evidence_cannot_replace_reserved_intent(self):
        first = self.arm()
        body, version = self.f.objects[self.a.failure_key]
        changed = json.loads(body); changed["classification"] = "order-replay"
        self.f.objects[self.a.failure_key] = encoded(changed), version
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(self.f.record["rollbackAttempt"], first)

    def test_new_task_or_candidate_does_not_rearm_an_existing_slot(self):
        slot = self.arm()
        self.f.record["deploymentTask"]["taskId"] = "0" * 32
        with self.assertRaises(ValueError): self.arm()
        self.f.record["deploymentTask"] = deepcopy(slot["origin"]["deploymentTask"])
        self.f.record["releaseCandidate"]["id"] = "0" * 64
        with self.assertRaises(ValueError): self.arm()


if __name__ == "__main__": unittest.main()
