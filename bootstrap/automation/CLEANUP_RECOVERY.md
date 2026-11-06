# Cleanup incident recovery

Use this runbook for one **existing admitted** staging session in account
`058114477594`, region `us-east-1`. It does not authorize a new deployment,
extend a deadline, or remove retained automation/backend resources. Keep the
deadline cleanup schedule and watchdog enabled while investigating.

## 1. Retain the exact incident identity

Record the session ID, immutable execution S3 VersionId, infra source SHA,
cleanup workflow run URL, and any returned CodeBuild build ID. Preserve private
inputs, Terraform state/lock, saved plans and diagnostic logs in restricted
storage. Do not upload private inputs, VPN keys, Cloudflare tokens, or Secret
values to issues or workflow summaries.

Relevant versioned objects in the admitted control bucket:

| Object | Evidence |
| --- | --- |
| `executions/<session>.json` at the admitted VersionId | Immutable scope, budget, source and deadline |
| `automation/staging/session.json` | Current execution/version, lease and dispatch checkpoint |
| `private/staging/<session>/inputs.json` | Private launch/certificate/Secret/DNS intents; read again after the manager is terminal |
| `cleanup/<session>/manager-<dispatch-id>.json` | Cleanup-owned exact manager termination receipt |
| `cleanup/<session>/executor-proof.json` | Exact launch-token/instance termination or PREPARED-not-invoked proof |
| `cleanup/<session>/{certificate-proof,secret-proof,shop-dns-observed}.json` | Positive resource identity retained before deletion |
| `cleanup/<session>/diagnostics/` | Private teardown logs and saved plan diagnostics when uploaded by the worker |
| `cleanup/<session>/inventory-<sha256>.json`, `complete.json` | Content-addressed inventory and completion receipt |

A file name, a lease timeout or a failed GitHub run does not prove that its AWS
manager or EC2/SSM child has stopped. Do not edit these objects to manufacture
terminal state, change CALLING to PREPARED, release a lease, or unlock Terraform
while its owner might still be running.

## 2. Continue observing a known cleanup build

For a still-running GitHub job, observe the same run:

```powershell
gh run watch <existing-run-id> --repo cloud-computing-microservice/techx-infra --exit-status
```

If the GitHub observation ended with `CLEANUP_RUNNING`, or the launcher response
was lost but an exact cleanup build has since been positively identified, start
a **new first-attempt observation workflow** on current main:

```powershell
gh workflow run staging-destroy.yml --repo cloud-computing-microservice/techx-infra --ref main `
  -f 'session_id=<existing-session>' `
  -f 'execution_version=<admitted-S3-VersionId>' `
  -f 'observe_build_id=techx-automation-cleanup:<existing-build-uuid>'
gh run list --repo cloud-computing-microservice/techx-infra --workflow staging-destroy.yml --limit 5
```

Replace placeholders with retained evidence, then observe the new run's URL/ID.
Do not use GitHub **Re-run jobs**: the caller intentionally requires
`run_attempt == 1`. Deployment flags may remain false and an expired deployment
window does not disable cleanup observation.

With `observe_build_id` set, the entry point **never invokes Lambda**, starts or
stops a build, repairs a marker, or writes state. It validates the caller,
current session/version, exact project/role/environment and build handle before
polling. A changed session, missing/foreign build, API error, failed worker or
unverified inventory fails closed. Another observation timeout retains the same
handle; continue observing it, do not infer a need to restart it.

Even `SUCCEEDED` is insufficient without a matching idle `DESTROYED` checkpoint,
version/hash-verified empty inventory and protected completion marker. The
existing bounded observation loop checks all three.

## 3. Investigate an unknown dispatch or stopped worker

Use an authorized read-only incident identity and verify its account first:

```powershell
$incidentProfile = '<authorized-profile>'
$incidentIdentity = aws sts get-caller-identity --profile $incidentProfile --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $incidentIdentity.Account -ne '058114477594') { throw 'Wrong incident account' }
```

For a retained cleanup handle, inspect metadata without dumping the environment
or credentials:

```powershell
aws codebuild batch-get-builds --profile $incidentProfile --region us-east-1 `
  --ids 'techx-automation-cleanup:<existing-build-uuid>' `
  --query '{missing:buildsNotFound,builds:builds[].{id:id,status:buildStatus,complete:buildComplete,project:projectName,role:serviceRole,logs:logs.deepLink}}' `
  --output json
```

Without a handle, use the CodeBuild project inventory and restricted service
logs to identify the exact build. Match `TECHX_SESSION_ID`,
`TECHX_EXECUTION_VERSION`, bucket, KMS key and fixed service role to the immutable
execution. A project name or approximate start time alone is insufficient. The
observation workflow independently rechecks the build; it cannot choose an
arbitrary project or silently attach to a different session.

| Unresolved boundary | Evidence and next action |
| --- | --- |
| Manager `StartBuild` CALLING | Preserve dispatch ID/operation and execution version. Cleanup searches bounded CodeBuild history for one exact environment match, then positively observes/stops that manager. Empty, truncated, duplicate or unavailable inventory remains unknown; no blind StartBuild replay. |
| Executor `RunInstances` CALLING | Preserve exact client token, manager ARN, session tags and any known instance ID. Stop/observe the manager, then read the private snapshot again. Cleanup must find the owned launch token and await instance termination before persisting its proof. A stopped manager alone does not stop EC2/SSM children. |
| Executor PREPARED | Only protocol version 1, a positively terminal manager and a fresh unchanged PREPARED snapshot with no instance reference/scoped instance can prove the call was never made. The cleanup producer, not the operator, writes `launchNotInvoked`. Legacy intent or lost checkpoint cannot use this shortcut. |
| Certificate request/import CALLING | Match exact session/purpose tags or an already retained ARN; use `DescribeCertificate` and tag checks. Cleanup writes the positive ARN receipt before deletion. Empty tag search without a prior receipt cannot establish non-creation. |
| Secret create CALLING | Match the exact session/purpose name, account/region ARN, tags and deterministic version token through metadata only. Never retrieve the Secret value to diagnose ownership, and never recreate/rotate it to resolve ambiguity. ResourceNotFound without a positive ARN receipt remains unknown for CALLING. |
| Cloudflare create CALLING | Match only `shop.dinhminhkhoa.id.vn`, exact admitted CloudFront target, DNS-only, and comment `techx:<session>`. Leave an owned live record for cleanup to persist `shop-dns-observed.json` before deletion; never substitute another session's record or touch the apex A record. Absence without positive creation proof remains unknown. |

If the service no longer exposes the original resource and no protected receipt
exists, retain the incident as **unresolved**, preserve service audit evidence
and escalate reconciliation to the account owner. This source version has no
operator override that converts such absence into completion. Do not fabricate
proof JSON or a new resource merely to satisfy the verifier.

## 4. Resume cleanup after reconciliation

The AWS watchdog/launcher reuses an in-progress worker and may start cleanup
again after a **positively terminal** prior worker. A GitHub observation timeout
is not that evidence. Resolve permission/service faults without broadening the
workload scope; preserve original session, execution version, budget and deadline.

If an explicit cleanup request is needed after reconciliation, use a new
first-attempt workflow with the same admitted identity and **omit**
`observe_build_id`:

```powershell
gh workflow run staging-destroy.yml --repo cloud-computing-microservice/techx-infra --ref main `
  -f 'session_id=<existing-session>' -f 'execution_version=<admitted-S3-VersionId>'
```

This is a mutating cleanup request. It is appropriate only after reconciling
an unknown dispatch; it is not a replacement for read-only observation. The
launcher still checks running workers. The cleanup producer still requires
positive stop/termination evidence before takeover. If `DESTROYED` was committed
but its marker was lost, only the cleanup producer can recover the marker from
the original versioned inventory and service timestamp; observe-only mode never
does so.

## 5. Close the incident with scoped evidence

Keep the verified completion result, exact inventory VersionId/hash and original
service timestamp, terminal build/instance evidence, DNS convergence evidence,
and diagnostics references. Verify every list in `contracts/inventory-fields.json`
is present and empty for this workload; an error or missing field is not zero.
List retained backend/control resources, AMI/snapshots, alerting and their ongoing
costs separately. A successful workload teardown does not delete or stop charges
for those retained resources. Prove notification delivery and failure recovery
in G1/G5 before calling the whole automation plan accepted.
