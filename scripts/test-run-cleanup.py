import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation_inputs import FIXED
import run_cleanup


class CleanupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        environment = patch.dict(os.environ, CLOUDFLARE_SECRET_ARN="arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/cloudflare-TestOnly")
        environment.start()
        self.addCleanup(environment.stop)
        self.private = dict(schemaVersion=1, sessionId="staging-123456", dnsMutationAttempted=False, validationDnsPlans=[],
            terraformVars=dict(FIXED, budget_alert_email="owner@example.invalid", public_access_cidrs=["8.8.8.8/32"]))
        class Missing(Exception): pass
        def absent_cluster(**kwargs): raise Missing()
        def credentials(**kwargs):
            self.events.append("shared-secret")
            return dict(SecretString=json.dumps(dict(zoneId="a" * 32, apiToken="fake-never-printed")))
        self.clients = dict(
            s3=SimpleNamespace(get_object=lambda **kwargs: dict(Body=io.BytesIO(json.dumps(self.private).encode()))),
            secretsmanager=SimpleNamespace(get_secret_value=credentials, list_secrets=lambda **kwargs: dict(SecretList=[])),
            ec2=SimpleNamespace(describe_instances=lambda **kwargs: dict(Reservations=[])),
            eks=SimpleNamespace(exceptions=SimpleNamespace(ResourceNotFoundException=Missing), describe_cluster=absent_cluster))
        self.inventory = dict(accountId="058114477594", region="us-east-1", environment="staging")
        for key in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[key] = []
        def subprocess_run(command, **kwargs):
            if command[-1].endswith("public-dns.py"): self.events.append("dns-proof"); return SimpleNamespace(returncode=0, stdout="")
            if any("destroy-domain-vpn-environment.ps1" in part for part in command): self.events.append("teardown"); return SimpleNamespace(returncode=0)
            if any("domain-vpn-inventory.ps1" in part for part in command):
                self.events.append("inventory")
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.inventory))
            raise AssertionError(f"Unexpected process in isolated worker test: {command[0]}")
        self.runner = patch("run_cleanup.subprocess.run", side_effect=subprocess_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.ip = patch("run_cleanup.urlopen", side_effect=lambda *a, **k: io.BytesIO(b"8.8.8.8\n"))
        self.ip.start()
        self.addCleanup(self.ip.stop)

    def work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            args = SimpleNamespace(bucket="control-bucket")
            run_cleanup.work(args, self.clients, dict(sessionId="staging-123456"), path)
            self.assertTrue((path / "inventory.json").is_file())

    def test_no_cloudflare_credential_dependency_before_any_dns_mutation(self):
        self.work()
        self.assertNotIn("shared-secret", self.events)
        self.assertLess(self.events.index("dns-proof"), self.events.index("teardown"))

    def test_storefront_removal_precedes_edge_teardown(self):
        self.private["dnsMutationAttempted"] = True
        with patch("run_cleanup.remove_owned_shop", side_effect=lambda *args: self.events.append("remove-shop")):
            self.work()
        self.assertLess(self.events.index("remove-shop"), self.events.index("teardown"))

    def test_validation_dns_is_removed_only_after_certificate_teardown(self):
        name, target = "_abc123.shop.dinhminhkhoa.id.vn", "_abc.def.acm-validations.aws"
        self.private["validationDnsPlans"] = [dict(name=name, target=target)]
        client = SimpleNamespace(records=lambda _: [dict(id="b" * 32, content=target, comment="techx:staging-123456")],
                                 delete=lambda ownership: self.events.append("remove-validation"))
        with patch("run_cleanup.CloudflareDNS", return_value=client): self.work()
        self.assertLess(self.events.index("teardown"), self.events.index("shared-secret"))
        self.assertLess(self.events.index("teardown"), self.events.index("remove-validation"))

    def test_wrong_private_scope_never_starts_teardown(self):
        self.private["terraformVars"]["project"] = "other"
        with self.assertRaises(ValueError): self.work()
        self.assertFalse(self.events)


if __name__ == "__main__": unittest.main()
