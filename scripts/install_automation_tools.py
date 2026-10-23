"""Install the committed, checksum-pinned Linux toolchain without executing installers."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
from urllib.request import urlopen
import zipfile


def binary(payload, spec):
    if hashlib.sha256(payload).hexdigest() != spec["sha256"]:
        raise ValueError("Toolchain download checksum mismatch")
    if spec["format"] == "binary": return payload
    if spec["format"] == "zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive: return archive.read(spec["member"])
    if spec["format"] == "tar":
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.getmember(spec["member"])
            if not member.isfile(): raise ValueError("Toolchain archive member is not a regular file")
            return archive.extractfile(member).read()
    raise ValueError("Unsupported toolchain archive format")


def install(directory):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("This toolchain is only for the Linux amd64 executor")
    specs = json.loads(Path(__file__).with_name("toolchain-linux-amd64.json").read_text())
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in specs.items():
        with urlopen(spec["url"], timeout=60) as response:
            payload = response.read(100 * 1024 * 1024 + 1)
        if len(payload) > 100 * 1024 * 1024: raise ValueError("Toolchain download exceeds the size policy")
        executable = directory / name
        executable.write_bytes(binary(payload, spec))
        executable.chmod(0o755)
    return {name: spec["version"] for name, spec in specs.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/usr/local/bin"))
    args = parser.parse_args()
    print(json.dumps(install(args.directory), sort_keys=True))
