import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from package_executor_image import FILES, package

spec = importlib.util.spec_from_file_location("verify_executor_ssm", FILES["verify_executor_ssm.py"])
ssm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssm)


class AgentCompatibilityTests(unittest.TestCase):
    def test_only_supported_agent_versions_pass(self):
        with tempfile.TemporaryDirectory() as base:
            agent = Path(base) / "amazon-ssm-agent"
            agent.touch()
            for version in ("3.3.2746.0", "3.3.4000.0", "4.0.0.0"):
                with patch.object(ssm.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SSM Agent version: " + version + "\n", "")) as run:
                    self.assertIn(version, ssm.verify((agent,)))
                    run.assert_called_once_with([str(agent), "-version"], check=True, capture_output=True, text=True, timeout=15)
            for version in ("3.3.2745.9", "3.2.9999.0", "3.3.2746", "unknown", "3.3.2746.0 injected"):
                with patch.object(ssm.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SSM Agent version: " + version, "")):
                    with self.assertRaises(RuntimeError): ssm.verify((agent,))

    def test_missing_failed_and_unresponsive_agents_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as base:
            agent = Path(base) / "amazon-ssm-agent"
            with self.assertRaises(RuntimeError): ssm.verify((agent,))
            agent.touch()
            for error in (subprocess.CalledProcessError(1, str(agent)), subprocess.TimeoutExpired(str(agent), 15)):
                with patch.object(ssm.subprocess, "run", side_effect=error):
                    with self.assertRaises(type(error)): ssm.verify((agent,))


class ImagePackageTests(unittest.TestCase):
    def test_package_preserves_hashes_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as base:
            output = Path(base) / "bootstrap"
            proof = package(output)
            for line in (output / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ")
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(hashlib.sha256((output / "SHA256SUMS").read_bytes()).hexdigest(), proof["loaderSha256"])
            self.assertTrue(all(b"\r\n" not in (output / name).read_bytes() for name in FILES))
            with self.assertRaises(FileExistsError): package(output)

    def test_package_hashes_match_actual_terraform_configuration(self):
        root = Path(__file__).parents[1]
        env = dict(os.environ, TF_DATA_DIR=str(root / ".terraform-offline/bootstrap/automation"))
        result = subprocess.run(["terraform", f"-chdir={root / 'bootstrap/automation'}", "console", "-var=bucket_name=techx-test-control"],
            input="jsonencode({loaderSha256=local.executor_loader_sha256,imageBuildSha256=local.executor_image_asset_sha256})\n",
            env=env, capture_output=True, text=True, check=True)
        expected = json.loads(json.loads(result.stdout.strip()))
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(package(Path(base) / "bootstrap"), expected)


if __name__ == "__main__": unittest.main()
