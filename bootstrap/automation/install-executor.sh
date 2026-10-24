#!/usr/bin/env bash
# Executed by Image Builder only after the complete bootstrap ZIP SHA256 check.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
source /etc/os-release
test "$ID" = ubuntu && test "$VERSION_ID" = 24.04 && test "$(uname -m)" = x86_64
source_asset="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
asset="$(mktemp -d /tmp/techx-executor.XXXXXX)"
cp -a "$source_asset/." "$asset/"
test -f "$asset/SHA256SUMS"
(cd "$asset" && sha256sum --check --status SHA256SUMS)
# Public control subnet allows HTTPS only; Ubuntu repositories support HTTPS.
if test -f /etc/apt/sources.list.d/ubuntu.sources; then
  sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources
fi
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl unzip gnupg python3 python3-venv openssl git tar \
  libicu74 libunwind8 libssl3t64 libgssapi-krb5-2 zlib1g libstdc++6 libgcc-s1
install -d -m 0755 /opt/techx/bootstrap /opt/microsoft/powershell/7 /var/lib/techx
python3 -m venv /opt/techx/venv
/opt/techx/venv/bin/python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r "$asset/executor-python.lock"
cp "$asset"/{automation_bundle.py,cleanup_launcher.py,cleanup_loader.py,deployment_task_loader.py,SHA256SUMS} /opt/techx/bootstrap/
chmod 0644 /opt/techx/bootstrap/*
/opt/techx/venv/bin/python "$asset/install_automation_tools.py" --directory /usr/local/bin
# Download fixed official releases; verify hashes before extracting/executing.
ASSET_DIR="$asset" /opt/techx/venv/bin/python - <<'PY'
import hashlib, json, os
from pathlib import Path
from urllib.request import urlopen
asset = Path(os.environ['ASSET_DIR'])
specs = json.loads((asset / 'executor-image-tools.json').read_text())
for name, spec in specs.items():
    with urlopen(spec['url'], timeout=60) as response:
        payload = response.read(150 * 1024 * 1024 + 1)
    if len(payload) > 150 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != spec['sha256']:
        raise ValueError('Pinned image tool checksum/size mismatch: ' + name)
    (asset / (name + '.archive')).write_bytes(payload)
with urlopen(specs['awscli']['signatureUrl'], timeout=30) as response:
    (asset / 'awscli.sig').write_bytes(response.read(64 * 1024))
PY
keyring="$(mktemp -d /tmp/techx-aws-keyring.XXXXXX)"
gpg --batch --homedir "$keyring" --import "$asset/executor-aws-cli-public-key.asc"
gpg --batch --homedir "$keyring" --with-colons --fingerprint | grep -q 'FB5DB77FD5C118B80511ADA8A6310ACC4672475C'
gpg --batch --homedir "$keyring" --verify "$asset/awscli.sig" "$asset/awscli.archive"
unzip -q "$asset/awscli.archive" -d "$asset/awscli-install"
"$asset/awscli-install/aws/install" --update
tar -xzf "$asset/powershell.archive" -C /opt/microsoft/powershell/7
chmod +x /opt/microsoft/powershell/7/pwsh
ln -sfn /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh
aws --version 2>&1 | grep -q '^aws-cli/2.34.28 '
test "$(pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')" = 7.6.6
terraform version -json | /opt/techx/venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["terraform_version"] == "1.15.5"'
helm version --short | grep -q '^v4.1.4'
kubectl version --client=true -o json | /opt/techx/venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["clientVersion"]["gitVersion"] == "v1.35.0"'
/opt/techx/venv/bin/python -m compileall -q /opt/techx/bootstrap
install -m 0644 "$asset/executor-image-tools.json" /opt/techx/image-tools.json
install -m 0644 "$asset/verify_executor_ssm.py" /opt/techx/verify_executor_ssm.py
(cd /opt/techx/bootstrap && sha256sum --check --status SHA256SUMS)
# Disable inbound access in the baked runtime image; management uses fixed SSM.
systemctl disable ssh.service ssh.socket || true
systemctl stop ssh.service ssh.socket || true
apt-get clean
case "$asset" in /tmp/techx-executor.*) rm -rf -- "$asset" ;; *) exit 1 ;; esac
case "$keyring" in /tmp/techx-aws-keyring.*) rm -rf -- "$keyring" ;; *) exit 1 ;; esac
