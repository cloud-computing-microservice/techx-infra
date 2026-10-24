from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

BOOTSTRAP = Path(__file__).parents[1] / "bootstrap/automation"
sys.path.insert(0, str(BOOTSTRAP))
from cleanup_loader import load


class LoaderTests(unittest.TestCase):
    def setUp(self):
        source = {name: "a" * 40 for name in ("techx-infra", "techx-chart", "techx-platform")}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in {"source.json": json.dumps(source).encode(), "techx-infra/README.md": b"source"}.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
        self.payload = buffer.getvalue()
        digest = hashlib.sha256(self.payload).hexdigest()
        now = datetime.now(timezone.utc)
        self.record = dict(sessionId="staging-123456", account="058114477594", region="us-east-1", environment="staging",
            cleanupStartAt=now.isoformat(), deadline=(now + timedelta(hours=1)).isoformat(), sourceRevisions=source,
            cleanupBundle=dict(key=f"bundles/{digest}.tar.gz", sha256=digest, versionId="bundle-version"))
        self.calls = []
        def get_object(**kwargs):
            self.calls.append(kwargs)
            data = json.dumps(self.record).encode() if kwargs["Key"].startswith("executions/") else self.payload
            return dict(Body=io.BytesIO(data), VersionId=kwargs["VersionId"])
        self.client = SimpleNamespace(get_object=get_object)

    def test_exact_versions_and_checksum_are_required_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            infra = load(self.client, "control-bucket", "staging-123456", "execution-version", Path(directory) / "work")
            self.assertTrue((infra / "README.md").exists())
        self.assertEqual([call["VersionId"] for call in self.calls], ["execution-version", "bundle-version"])
        self.assertTrue(all(call["ExpectedBucketOwner"] == "058114477594" for call in self.calls))

    def test_altered_source_manifest_is_not_run(self):
        self.record["sourceRevisions"]["techx-infra"] = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): load(self.client, "control-bucket", "staging-123456", "execution-version", Path(directory) / "work")


if __name__ == "__main__": unittest.main()
