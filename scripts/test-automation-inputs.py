import unittest
from automation_inputs import ROLE, validate_variables


class InputTests(unittest.TestCase):
    def test_contract_and_cleanup_identity_are_preserved(self):
        variables = dict(cleanup_executor_role_arn=ROLE, budget_alert_email="owner@example.invalid", public_access_cidrs=["8.8.8.8/32"])
        self.assertEqual(validate_variables(variables), variables)
        self.assertEqual(validate_variables(dict(variables, budget_limit_usd=60))["budget_limit_usd"], 60)
        for key, value in (("project", "other"), ("region", "other"), ("environment", "production"), ("node_instance_type", "m7i.large"),
                           ("node_disk_size_gib", 100), ("public_access_cidrs", ["0.0.0.0/0"]), ("allow_temporary_public_access_cidr", True),
                           ("unexpected", "value"), ("cleanup_executor_role_arn", "other"), ("public_certificate_arn", "arn:other"),
                           ("budget_limit_usd", 0), ("budget_limit_usd", 81), ("budget_limit_usd", True), ("budget_limit_usd", float("nan"))):
            with self.subTest(key=key), self.assertRaises(ValueError): validate_variables(dict(variables, **{key: value}))


if __name__ == "__main__": unittest.main()
