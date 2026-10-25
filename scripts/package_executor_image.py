"""Emit the same normalized bootstrap files/hashes as the Image Builder recipe."""
import argparse
import hashlib
import json
from pathlib import Path

BOOTSTRAP = Path(__file__).parents[1] / "bootstrap/automation"
FILES = {
    "automation_bundle.py": Path(__file__).with_name("automation_bundle.py"),
    "cleanup_launcher.py": BOOTSTRAP / "cleanup_launcher.py",
    "cleanup_loader.py": BOOTSTRAP / "cleanup_loader.py",
    "deployment_task_loader.py": BOOTSTRAP / "deployment_task_loader.py",
    "install-executor.sh": BOOTSTRAP / "install-executor.sh",
    "install_automation_tools.py": Path(__file__).with_name("install_automation_tools.py"),
    "toolchain-linux-amd64.json": Path(__file__).with_name("toolchain-linux-amd64.json"),
    "executor-image-tools.json": BOOTSTRAP / "executor-image-tools.json",
    "executor-python.lock": BOOTSTRAP / "executor-python.lock",
    "executor-aws-cli-public-key.asc": BOOTSTRAP / "executor-aws-cli-public-key.asc",
    "verify_executor_ssm.py": BOOTSTRAP / "verify_executor_ssm.py",
}
LOADER = {"automation_bundle.py", "cleanup_launcher.py", "cleanup_loader.py", "deployment_task_loader.py"}


def package(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, source in FILES.items():
        payload = source.read_bytes().decode("utf-8").replace("\r\n", "\n").encode()
        (directory / name).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    manifest = "".join(f"{hashes[name]}  {name}\n" for name in sorted(LOADER)).encode()
    (directory / "SHA256SUMS").write_bytes(manifest)
    build = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return dict(loaderSha256=hashlib.sha256(manifest).hexdigest(), imageBuildSha256=hashlib.sha256(build).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output), sort_keys=True))
