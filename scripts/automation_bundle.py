"""Create deterministic tracked-source bundles and extract immutable cleanup code safely."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

REPOSITORIES = {"techx-infra", "techx-chart", "techx-platform"}
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 50000


def allowed_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or not path.parts or ".." in path.parts or "\\" in name or ":" in name
            or (path.parts[0] not in REPOSITORIES and name != "source.json")):
        raise ValueError("Unsafe or unexpected bundle path")
    if any(part.lower() in (".git", "node_modules", ".terraform", ".terraform-offline", ".terraform-helm", ".plans", ".pki", ".venv") for part in path.parts):
        raise ValueError("Generated/private directory is forbidden in a source bundle")
    if any(part.lower().startswith('.env') and part.lower() != '.env.example' for part in path.parts):
        raise ValueError("Runtime environment files are forbidden in a source bundle")
    if re.search(r"\.(?:key|pem|crt|ovpn|tfstate(?:\..*)?|tfplan|p12|pfx)$|\.private\.", name.lower()):
        raise ValueError("Private artifact is forbidden in a source bundle")
    return path


def pack(repositories: dict[str, tuple[Path, str]], output: Path):
    if set(repositories) != REPOSITORIES:
        raise ValueError("Bundle must include exactly the three TechX repositories")
    manifest = {}
    total = 0
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            for name in sorted(repositories):
                directory, revision = repositories[name]
                if not re.fullmatch(r"[a-f0-9]{40}", revision):
                    raise ValueError("Full source commit SHA is required")
                data = subprocess.run(["git", "-C", str(directory), "archive", "--format=tar", f"--prefix={name}/", revision],
                                      check=True, capture_output=True).stdout
                manifest[name] = revision
                with tarfile.open(fileobj=io.BytesIO(data)) as source:
                    for member in source:
                        allowed_path(member.name)
                        if not member.isfile() and not member.isdir():
                            raise ValueError("Source bundles cannot contain symlinks or special files")
                        total += member.size
                        if total > MAX_TOTAL_BYTES: raise ValueError("Source archive exceeds the size policy")
                        member.mtime, member.uid, member.gid, member.uname, member.gname = 0, 0, 0, "", ""
                        member.pax_headers = {}
                        archive.addfile(member, source.extractfile(member) if member.isfile() else None)
            metadata = json.dumps(manifest, sort_keys=True).encode()
            member = tarfile.TarInfo("source.json")
            member.size = len(metadata)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(metadata))
    if output.stat().st_size > MAX_BUNDLE_BYTES: raise ValueError("Compressed bundle exceeds the size policy")
    return hashlib.sha256(output.read_bytes()).hexdigest()


def extract(path: Path, destination: Path, expected_sha256: str):
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256): raise ValueError("Expected SHA-256 is required")
    if path.stat().st_size > MAX_BUNDLE_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Cleanup source bundle checksum/size mismatch")
    if destination.exists(): raise ValueError("Extraction destination must be new")
    seen, total = set(), 0
    # Validate the entire archive before creating the output directory.
    with tarfile.open(path, "r:gz") as archive:
        members = []
        for member in archive:
            if len(members) >= MAX_MEMBERS: raise ValueError("Too many source archive members")
            members.append(member)
            relative = allowed_path(member.name)
            normalized = str(relative)
            if normalized in seen: raise ValueError("Duplicate archive path")
            seen.add(normalized)
            if not member.isfile() and not member.isdir(): raise ValueError("Links and special files are forbidden")
            total += member.size
            if total > MAX_TOTAL_BYTES: raise ValueError("Expanded bundle exceeds size policy")
        if "source.json" not in seen: raise ValueError("Source manifest missing")
        source_metadata = archive.extractfile("source.json")
        manifest = json.load(source_metadata)
        if set(manifest) != REPOSITORIES or any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in manifest.values()):
            raise ValueError("Invalid bundled source manifest")
        destination.mkdir(parents=True)
        for member in members:
            target = destination.joinpath(*allowed_path(member.name).parts)
            if member.isdir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("xb") as stream:
                    while chunk := source.read(1024 * 1024): stream.write(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--infra-sha", required=True)
    parser.add_argument("--chart-sha", required=True)
    parser.add_argument("--platform-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repositories = {"techx-infra": (args.root / "techx-infra", args.infra_sha),
                    "techx-chart": (args.root / "techx-chart", args.chart_sha),
                    "techx-platform": (args.root / "techx-platform", args.platform_sha)}
    print(json.dumps(dict(sha256=pack(repositories, args.output))))
