import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from automation_bundle import REPOSITORIES, extract, pack


class BundleTests(unittest.TestCase):
    def test_real_git_archive_is_deterministic_and_excludes_untracked_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories = {}
            for name in REPOSITORIES:
                repo = root / name
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / "README.md").write_text("tracked source\n")
                subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "commit", "-qm", "baseline"], check=True)
                sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                repositories[name] = repo, sha
                (repo / "credentials.private.json").write_text("must not be bundled")
            first = pack(repositories, root / "one.tar.gz")
            self.assertEqual(first, pack(repositories, root / "two.tar.gz"))
            manifest = extract(root / "one.tar.gz", root / "output", first)
            self.assertEqual(set(manifest), REPOSITORIES)
            self.assertFalse(list((root / "output").rglob("*private*")))

    def test_bad_checksum_and_archive_traversal_links_private_paths_never_extract(self):
        for unsafe in ("../outside", "/absolute", "techx-infra/../outside", "techx-infra/secret.key", "techx-infra/secret.KEY",
                       "techx-infra/terraform.tfstate.backup", "techx-platform/.env.local", "techx-infra/.terraform-offline/cache", "link"):
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "unsafe.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo(unsafe)
                    if unsafe == "link":
                        member.name = "techx-infra/link"
                        member.type, member.linkname = tarfile.SYMTYPE, "../../outside"
                    archive.addfile(member, io.BytesIO())
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ValueError): extract(path, root / "output", sha)
                self.assertFalse((root / "output").exists())
                with self.assertRaises(ValueError): extract(path, root / "output", "0" * 64)


if __name__ == "__main__": unittest.main()
