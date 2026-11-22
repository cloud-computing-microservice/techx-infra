# EC2/SSM automation v2: initial contracts

These are offline validators, not a deployed backend or an authorization
service. The current workflows still use v1. Do not enable automation, admit a
session, or run infrastructure from these examples. A new reviewed cloud window
and saved-plan approval are required separately.

## Immutable execution

`scripts/execution_v2.py` accepts exactly these execution fields:

| Field | Contract |
| --- | --- |
| `schemaVersion`, `executorBackend` | Integer `2`, string `ec2-ssm` |
| `account`, `region`, `environment` | `058114477594`, `us-east-1`, `staging` |
| `sessionId` | `staging-v2-` followed by 1–60 lowercase letters, digits or hyphens |
| `sourceRevisions` | Exactly `techx-infra`, `techx-platform`, `techx-chart`, each a full lowercase commit SHA |
| `approvedBudgetUsd` | Decimal **string**, positive, at most two decimal places, maximum `60.00` |
| `startedAt`, `cleanupStartAt`, `deadline` | Canonical UTC `YYYY-MM-DDTHH:MM:SSZ`; ordered; at most ten hours total and at least two hours reserved for cleanup |
| `initialEpoch` | Integer `1`; subsequent fencing belongs to the mutable session |

The execution reference has exactly `bucket`, `key`, `versionId`, `sha256`.
The bucket is `techx-automation-058114477594-us-east-1`; the key is
`automation/v2/staging/executions/<sessionId>.json`. No current pointer, v1
prefix, null/latest version or caller-chosen bucket is accepted.

The private reader must fetch that exact S3 version. `decode_execution` hashes
the original bytes before JSON parsing, bounds the payload and rejects duplicate
keys. Successful parsing does not prove source CI, approval, current lease,
cleanup readiness, or migration closure. Admission must verify each gate.

The minimal mutable session envelope contains `schemaVersion`,
`executorBackend`, `sessionId`, `executionReference`, `status`, `leaseEpoch`.
This is a parser, not a state-transition API. In particular, parsing the string
`DESTROYED` does not grant permission to write a completion marker.

## Trusted identity inputs

`scripts/worker_identity.py` requires service observations from trusted callers;
passing JSON from a workflow payload is **not** authentication.

- `manager_identity` binds independently verified OIDC claims, STS caller
  identity and trusted GitHub run/job context to the admitted infra SHA. It
  requires the reviewed immutable owner/repository IDs (`328679702`,
  `1322975868`), main, first attempt, exact workflow/environment and exact v2
  manager role session. It does not verify JWT signatures or GitHub API history.
- `worker_identity` binds a positive EC2 observation and worker STS caller to the
  exact launch intent, execution version, epoch, purpose, template version,
  AMI, profile, subnet, security group, idempotency token and ownership tags.
  It requires IMDSv2, no SSH key, service launch time and observed public IPv4.
  Configuration authorization and fencing before launch are launcher duties.
- `validate_handle` binds the private typed task, worker and positive SSM command
  observation. It rejects cross-session/epoch/instance/document/task/command
  changes and arbitrary shell parameters. An expired dispatch window still
  permits observing the **same** command. This function neither dispatches nor
  declares a stage successful. CAS single consumption, lost-response recovery,
  document checksum enforcement at SendCommand, terminal invocation and durable
  result verification remain required in the runtime implementation.

Task/handle operation and stage are bound separately (for example,
`plan/foundation` versus `plan/edge`). Only the reviewed lifecycle pairs are
accepted; plan/apply attempts are positive bounded integers and all other tasks
require attempt one. A new plan attempt requires a separately admitted immutable
task; this contract never allows resending an uncertain consumed task.

Worker `launchEpoch` records its original registration. It is distinct from a
task's `leaseEpoch`, so the warm cleanup worker can survive a later cleanup fence.
The live CAS registry must authorize that fence and worker generation before a
new command is sent. Worker observations include `launchedAt` and `publicCidr`;
review/apply admission must compare the registered boot and network identity to
reject stop/start reuse or an IP change.

Additional contracts are still needed for plan/review, provisional cleanup,
finalizer proof and legacy migration.
Do not use a format-valid identity, service `Success` alone, or process exit code
as authority to advance the lifecycle.

## Launch recovery and durable results

The launch intent also pins `configurationReference`, an exact private version
at `automation/v2/staging/config/<purpose>.json`. The deterministic launch token
includes that reference. Configuration contents and resource permission policy
must still be checked by the launcher before it can create a worker.

`scripts/recovery_v2.py` provides pure checkpoint transitions:

- `prepare_launch` validates the immutable intent and returns `PREPARED`.
- `begin_launch` accepts only `PREPARED`, the current epoch and an open launch
  window. Deployment/probe launches stop at `cleanupStartAt`; bounded cleanup
  recovery may continue after the deadline. Persist `CALLING` with CAS **before**
  calling EC2. There is no reset/retry operation for uncertain `CALLING`.
- `observe_launch` requires the exact worker's trusted EC2/STS observations to
  resolve `CALLING`. Repeat observation is idempotent; another instance, boot or
  public IP cannot replace the recorded worker identity.

The checkpoint contains exactly `schemaVersion`, `launchToken`, `phase`,
`callStartedAt`, `instanceId`, `observedAt`, `workerIdentitySha256`.
It is not a launcher: API calls, CAS writes, service reconciliation, generation
retirement and bounded replacement proof belong to R03/R04.

`validate_successful_result` checks an exact-version private JSON receipt at
`automation/v2/staging/results/<sessionId>/<taskId>/<commandId>.json` together
with the already bound command and a positive `GetCommandInvocation` response.
Both AWS statuses must be terminal `Success`, response code must be integer zero,
the exact `runTask` document step must match, and service/result timestamps must
be ordered. Receipt timestamps preserve up to six fractional second digits.

Receipt fields are exactly `schemaVersion`, `handle`, `workerIdentitySha256`,
`outcome`, `startedAt`, `finishedAt`, `evidenceReference`. The evidence reference
must point to the task's private exact version at
`automation/v2/staging/evidence/<sessionId>/<taskId>.json`; its contents require
the operation-specific validator. Receipt parsing cannot replace fresh lease
fencing, approval, business acceptance or cleanup completion checks.

## Legacy isolation

`read_legacy` parses a bounded snapshot through the existing v1 validator and
returns a deep copy. It never writes, upgrades, repairs `CALLING`, invents a build
ID, or produces migration/completion evidence. Exact S3 version/hash provenance
must be verified by the migration reader before calling it.

`automation_session.validate` rejects v2 schema/backend values before any v1
lease or transition logic. Historical v1 workflows continue unchanged. The
separate v2 mutation store and protected cutover are not implemented here.

## Verification

Run `python scripts/test-execution-v2.py` from the infra repository. The same
suite runs in `scripts/verify.ps1` and the existing infrastructure CI workflow.
Run `python scripts/test-recovery-v2.py` for checkpoint and result regressions;
it also runs in the existing verification workflow.
Fixtures are synthetic offline data; they are not cloud execution evidence.
