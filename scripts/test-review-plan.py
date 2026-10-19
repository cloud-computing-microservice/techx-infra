"""Exercise plan review against unsafe real-world action combinations offline."""
import json
import pathlib
import subprocess
import sys
import unittest

REVIEWER = pathlib.Path(__file__).with_name("review-plan.py")


def change(address, actions, resource_type="aws_iam_role_policy"):
    return {"address": address, "type": resource_type, "mode": "managed", "change": {"actions": actions}}


class MaintenanceReviewTests(unittest.TestCase):
    def review(self, changes):
        return subprocess.run(
            [sys.executable, str(REVIEWER), "maintenance"],
            input=json.dumps({"resource_changes": changes}),
            text=True, capture_output=True,
        )

    def test_noop_is_repeatable(self):
        result = self.review([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resourceCount"], 0)

    def test_permits_only_expected_in_place_updates(self):
        changes = [
            change("module.order_persistence.aws_iam_role_policy.order_api", ["update"]),
            change("module.order_persistence.aws_vpc_endpoint.dynamodb", ["update"], "aws_vpc_endpoint"),
            change("module.cloudfront.aws_cloudfront_distribution.this[0]", ["update"], "aws_cloudfront_distribution"),
        ]
        result = self.review(changes)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_creation_deletion_replacement_and_scope_expansion(self):
        for actions in (["create"], ["delete"], ["delete", "create"], ["create", "delete"]):
            with self.subTest(actions=actions):
                self.assertNotEqual(self.review([change("module.order_persistence.aws_iam_role_policy.order_api", actions)]).returncode, 0)
        for address, resource_type in (
            ("module.eks.aws_eks_node_group.this", "aws_eks_node_group"),
            ("aws_iam_role_policy.unrelated", "aws_iam_role_policy"),
            ("aws_nat_gateway.new", "aws_nat_gateway"),
        ):
            with self.subTest(address=address):
                self.assertNotEqual(self.review([change(address, ["update"], resource_type)]).returncode, 0)


class TeardownReviewTests(unittest.TestCase):
    def review(self, stage, changes):
        return subprocess.run(
            [sys.executable, str(REVIEWER), stage],
            input=json.dumps({"resource_changes": changes}),
            text=True, capture_output=True,
        )

    def test_edge_deletes_only_edge_modules(self):
        allowed = change("module.cloudfront.aws_cloudfront_distribution.this[0]", ["delete"], "aws_cloudfront_distribution")
        self.assertEqual(self.review("teardown-edge", [allowed]).returncode, 0)
        forbidden = change("module.eks.aws_eks_cluster.this", ["delete"], "aws_eks_cluster")
        self.assertNotEqual(self.review("teardown-edge", [forbidden]).returncode, 0)

    def test_teardown_never_creates_updates_or_replaces(self):
        for stage in ("teardown-edge", "teardown-foundation"):
            self.assertEqual(self.review(stage, []).returncode, 0)
            for actions in (["create"], ["update"], ["delete", "create"], ["create", "delete"]):
                with self.subTest(stage=stage, actions=actions):
                    item = change("module.cloudfront.aws_cloudfront_distribution.this[0]", actions, "aws_cloudfront_distribution")
                    self.assertNotEqual(self.review(stage, [item]).returncode, 0)
        self.assertEqual(self.review("teardown-foundation", [change("module.eks.aws_eks_cluster.this", ["delete"], "aws_eks_cluster")]).returncode, 0)
        self.assertNotEqual(self.review("teardown-foundation", [change("unrelated.bucket", ["delete"], "aws_s3_bucket")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
