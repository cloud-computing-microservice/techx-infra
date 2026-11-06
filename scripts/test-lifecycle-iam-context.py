"""Declared identity-policy conditions must be present in negative IAM probes.

Uses mock-provider Terraform state, without modifying operator state or calling
AWS. This covers rendered JSON policies, not mocked policy-document data sources,
AWS-managed policy changes, effective permissions or live acceptance.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import unittest


def missing_context(state):
    resources = state["root_module"]["resources"]
    indexed = {row["address"]: row for row in resources}
    matrix = json.loads(state["outputs"]["lifecycle_iam_simulation_cases_json"]["value"])
    missing = []
    for name, group in matrix.items():
        role_name = group["principalArn"].split("/")[-1]
        roles = [row["values"] for row in resources if row["type"] == "aws_iam_role" and row["values"]["name"] == role_name]
        if len(roles) != 1: raise AssertionError("Expected exactly one declared role per matrix group")
        identifiers = {roles[0]["id"], roles[0]["name"]}
        keys = set()
        for row in resources:
            if row["values"].get("role") not in identifiers: continue
            if row["type"] == "aws_iam_role_policy":
                document = json.loads(row["values"]["policy"])
            elif row["type"] == "aws_iam_role_policy_attachment":
                address = row["address"].replace("aws_iam_role_policy_attachment.", "aws_iam_policy.")
                if address not in indexed: continue  # AWS-managed policies are outside this offline contract.
                document = json.loads(indexed[address]["values"]["policy"])
            else: continue
            for statement in document.get("Statement", []):
                for conditions in statement.get("Condition", {}).values():
                    keys.update(key.casefold() for key in conditions)
        for case in group["cases"]:
            if case["expected"] != "denied": continue
            provided = {entry["ContextKeyName"].casefold() for entry in case.get("context", [])}
            missing.extend((name, case["name"], key) for key in sorted(keys - provided))
    return missing


class ContextContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        directory = root / "bootstrap/automation"
        data = root / ".terraform-offline/bootstrap/automation"
        if not data.exists(): raise AssertionError("Initialize offline Terraform through scripts/verify.ps1 first")
        state_file = directory / "terraform.tfstate"
        before = state_file.read_bytes() if state_file.exists() else None
        result = subprocess.run(["terraform", "test", "-json", "-verbose"], cwd=directory,
            env=dict(os.environ, TF_DATA_DIR=str(data)), capture_output=True, timeout=120)
        if before != (state_file.read_bytes() if state_file.exists() else None): raise AssertionError("Operator state changed")
        if result.returncode: raise AssertionError("Terraform mock context contract failed: " + result.stderr.decode(errors="replace")[-600:])
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        states = [event["test_state"] for event in events if event.get("type") == "test_state" and event.get("@testrun") == "lifecycle_is_scoped_and_pinned"]
        if len(states) != 1: raise AssertionError("The exact lifecycle mock state must be observed")
        cls.state = states[0]

    def test_negative_probes_cover_declared_policy_conditions(self):
        self.assertEqual(missing_context(self.state), [])

    def test_missing_launch_context_is_detected(self):
        state = deepcopy(self.state)
        output = state["outputs"]["lifecycle_iam_simulation_cases_json"]
        matrix = json.loads(output["value"])
        case = next(case for case in matrix["manager"]["cases"] if case["name"] == "no-foreign-pass-role")
        case["context"] = [entry for entry in case["context"] if entry["ContextKeyName"] != "ec2:LaunchTemplate"]
        output["value"] = json.dumps(matrix)
        self.assertIn(("manager", "no-foreign-pass-role", "ec2:launchtemplate"), missing_context(state))


if __name__ == "__main__": unittest.main()
