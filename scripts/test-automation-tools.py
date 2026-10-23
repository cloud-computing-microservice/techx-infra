import hashlib
import io
import tarfile
import unittest
import zipfile

from install_automation_tools import binary


class ToolTests(unittest.TestCase):
    def test_only_pinned_archive_member_is_returned(self):
        zip_buffer, tar_buffer = io.BytesIO(), io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("terraform", b"trusted")
            archive.writestr("../unrelated", b"never-extracted")
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("linux-amd64/helm")
            member.size = len(b"trusted")
            archive.addfile(member, io.BytesIO(b"trusted"))
        for kind, payload, member in (("zip", zip_buffer.getvalue(), "terraform"), ("tar", tar_buffer.getvalue(), "linux-amd64/helm"),
                                      ("binary", b"trusted", "")):
            spec = dict(format=kind, sha256=hashlib.sha256(payload).hexdigest(), member=member)
            self.assertEqual(binary(payload, spec), b"trusted")
            with self.assertRaises(ValueError): binary(payload + b"tampered", spec)


if __name__ == "__main__": unittest.main()
