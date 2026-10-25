"""Reject an executor image whose SSM agent cannot use ENV_VAR parameters."""
from pathlib import Path
import re
import subprocess

MINIMUM = (3, 3, 2746, 0)
AGENTS = (Path("/usr/bin/amazon-ssm-agent"), Path("/snap/amazon-ssm-agent/current/amazon-ssm-agent"))


def verify(agents=AGENTS):
    agent = next((path for path in agents if path.is_file()), None)
    if agent is None:
        raise RuntimeError("Executor image has no installed SSM agent")
    result = subprocess.run([str(agent), "-version"], check=True, capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r"SSM Agent version: (\d+)\.(\d+)\.(\d+)\.(\d+)\s*", result.stdout.strip())
    if not match or tuple(map(int, match.groups())) < MINIMUM:
        raise RuntimeError("Executor image requires SSM Agent >= 3.3.2746.0 for TaskId ENV_VAR")
    return match.group(0).strip()


if __name__ == "__main__":
    print(verify())
