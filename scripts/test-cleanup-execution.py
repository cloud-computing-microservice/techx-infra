from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from cleanup_execution import (ACCOUNT, CLEANUP_ROLE, assert_inventory, fence_session,
                               restore_cleanup_access, stop_executor, terminate_probes)


class ExecutionTests(unittest.TestCase):
    def record(self, operation):
        now = datetime.now(timezone.utc)
        return dict(account=ACCOUNT, region="us-east-1", environment="staging", status="READY", sessionId="staging-123456",
                    cleanupStartAt=(now + timedelta(hours=1)).isoformat(), deadline=(now + timedelta(hours=2)).isoformat(),
                    lease=dict(owner="old", operation=operation, epoch=3), leaseEpoch=3)

    def test_inventory_requires_every_field_and_no_leftover_volume(self):
        inventory = dict(accountId=ACCOUNT, region="us-east-1", environment="staging")
        for name in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): inventory[name] = []
        assert_inventory(inventory)
        inventory["ebsVolumes"] = ["vol-leftover"]
        with self.assertRaises(ValueError): assert_inventory(inventory)
        inventory.pop("ebsVolumes")
        with self.assertRaises(ValueError): assert_inventory(inventory)

    def test_fencing_requires_infrastructure_worker_stop_proof(self):
        with self.assertRaises(ValueError): fence_session(self.record("deploy"), "cleanup:new")
        stopped = fence_session(self.record("deploy"), "cleanup:new", executor_stopped=True)
        self.assertNotIn("lease", stopped)
        self.assertEqual(stopped["status"], "FAILED_NEEDS_CLEANUP")
        publisher = fence_session(self.record("release"), "cleanup:new")
        self.assertEqual(publisher["leaseEpoch"], 4)

    def test_previous_executor_is_observed_terminal_before_takeover(self):
        owner = f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:abcdef-1234"
        statuses, stopped = ["IN_PROGRESS", "STOPPED"], []
        client = SimpleNamespace(batch_get_builds=lambda **kwargs: dict(builds=[dict(arn=owner, buildStatus=statuses.pop(0))]),
                                 stop_build=lambda **kwargs: stopped.append(kwargs))
        self.assertTrue(stop_executor(client, owner, "other", sleep=lambda _: None))
        self.assertEqual(stopped, [dict(id="techx-automation-lifecycle:abcdef-1234")])
        with self.assertRaises(ValueError): stop_executor(client, "arn:aws:codebuild:elsewhere", "other")

    def test_probe_ownership_is_verified_before_termination(self):
        tags = dict(Project="techx", Environment="staging", SessionId="staging-123456", Role="private-probe")
        instance = dict(InstanceId="i-probe", Tags=[dict(Key=k, Value=v) for k, v in tags.items()])
        terminated = []
        client = SimpleNamespace(describe_instances=lambda **kwargs: dict(Reservations=[dict(Instances=[instance])]),
                                 terminate_instances=lambda **kwargs: terminated.append(kwargs),
                                 get_waiter=lambda _: SimpleNamespace(wait=lambda **kwargs: None))
        terminate_probes(client, "staging-123456")
        self.assertEqual(terminated, [dict(InstanceIds=["i-probe"])])
        instance["Tags"][0]["Value"] = "unrelated"
        with self.assertRaises(ValueError): terminate_probes(client, "staging-123456")
        self.assertEqual(len(terminated), 1)

    def test_cleanup_network_recovery_never_opens_broad_cidr(self):
        calls = []
        class Missing(Exception): pass
        def missing(**kwargs): raise Missing()
        client = SimpleNamespace(exceptions=SimpleNamespace(ResourceNotFoundException=Missing),
            describe_cluster=lambda **kwargs: dict(cluster=dict(arn=f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/techx-staging",
                status="ACTIVE", resourcesVpcConfig=dict(endpointPrivateAccess=True, endpointPublicAccess=True, publicAccessCidrs=["1.1.1.1/32"]))),
            update_cluster_config=lambda **kwargs: calls.append(kwargs) or dict(update=dict(id="update-id")),
            describe_update=lambda **kwargs: dict(update=dict(status="Successful")), describe_access_entry=missing,
            create_access_entry=lambda **kwargs: calls.append(kwargs), associate_access_policy=lambda **kwargs: calls.append(kwargs))
        with self.assertRaises(ValueError): restore_cleanup_access(client, "10.52.0.1")
        self.assertFalse(calls)
        restore_cleanup_access(client, "8.8.8.8", sleep=lambda _: None)
        self.assertEqual(calls[0]["resourcesVpcConfig"]["publicAccessCidrs"], ["8.8.8.8/32"])
        self.assertEqual(calls[1]["principalArn"], CLEANUP_ROLE)
        self.assertEqual(calls[2]["clusterName"], "techx-staging")


if __name__ == "__main__": unittest.main()
