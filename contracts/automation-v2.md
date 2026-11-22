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
| `windowId`, `authorizationReference` | `window-gh-<runId>` and exact private version/hash at `automation/v2/windows/<windowId>.json` |

The execution reference has exactly `bucket`, `key`, `versionId`, `sha256`.
The bucket is `techx-automation-058114477594-us-east-1`; the key is
`automation/v2/staging/executions/<sessionId>.json`. No current pointer, v1
prefix, null/latest version or caller-chosen bucket is accepted.

The private reader must fetch that exact S3 version. `decode_execution` hashes
the original bytes before JSON parsing, bounds the payload and rejects duplicate
keys. Successful parsing does not prove source CI, approval, current lease,
cleanup readiness, or migration closure. Admission must verify each gate.

`bind_authorized_window` binds an execution to its protected window record:
account/region, three frozen revisions and shared budget must match; its time
range cannot extend the parent grant and its purpose must be explicitly allowed.
Migration and staging reference the same window ledger; they do not receive
separate USD 60 allowances. R05/R06 must authenticate the actual human review and
enforce aggregate build/control/migration/staging/recovery cost accounting.
Reading an expired record is allowed for cleanup/audit; runtime creation gates
must independently refuse expired/closed windows.

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

The existing v1 workflows have not been switched to these v2 contracts.
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

## Saved plan and human approval

`scripts/plan_v2.py` validates a private request at
`automation/v2/staging/reviews/<sessionId>/<stage>/<attempt>/request.json`.
It binds execution/epoch, stage/attempt, saved plan version/hash, timestamps and
the freshly regenerated runtime context:

- Three admitted revisions, budget and cleanup/deadline.
- Complete registered worker identity checksum, including VM boot/public `/32`
  and configuration reference. A changed boot/IP requires replan/review.
- Pinned backend/key/KMS, Terraform toolchain/config/provider-lock hashes.
- Current state version/checksum, lineage and serial.

The downloaded plan must match its exact reviewed S3 version, even if another
version has identical bytes. Resource-action scanning, cost checks, live lease,
readiness and single CAS consumption remain mandatory runtime gates.

An approval at `automation/v2/staging/approvals/<sessionId>/<requestSha256>.json`
binds the request's private version and independently verified GitHub history.
It requires a human reviewer on the reviewed owner/repository IDs, infra SHA,
first-attempt `staging-plan-review.yml` run and `staging-apply` environment.
Rechecking history may advance its verification time; it cannot change stable
reviewer/run/environment identity. Approval/request expiry is enforced before
apply. These functions cannot create approvals or authenticate review JSON:
R06 must collect real run/job/OIDC/API history, reject rejected/ambiguous reviews,
and publish only through the scoped approval role.

## Independent cleanup and finalization

`scripts/cleanup_v2.py` separates provisional worker receipts from final evidence:

1. `workload_zero` checks all 22 positively present empty inventory groups,
   matching execution/epoch/state version, and exact raw state bytes with an
   initialized lineage/serial and zero managed instances.
2. `compute_gone` checks the complete protected roster and fresh service facts.
   Unknown discovered instances, missing registered assets and unresolved
   `CALLING` launches prevent completion. Bounds are one deployment, one probe
   and up to three cleanup generations.
3. Each worker requires its singleton DescribeInstances result, positive AWS
   metadata and exact ownership/token. Provisional mode permits only the current
   cleanup owner to run. Final mode requires every worker terminal, no attached
   public IP/NIC/volume, and every registered volume/ENI absent.
4. `InvalidInstanceID.NotFound` alone is insufficient. It requires an exact
   registered private historical proof with a positive `terminated` response
   for the same instance/token/execution. [AWS documents that NotFound can occur
   while an instance still runs](https://docs.aws.amazon.com/ec2/latest/devguide/eventual-consistency.html).
   R04 must persist these terminal proofs through the finalizer-only prefix and
   use bounded service backoff. Unknown/missing proof stays incomplete.
5. `validate_final` checks fresh independently collected facts after the
   provisional receipt, live `DESTROYING` epoch, exact published fact versions
   and the actual dedicated finalizer STS principal. Worker/manager identity
   cannot seal completion. CAS sealing, protected DESTROYED/marker writes and
   duplicate recovery are still R04 runtime duties.

The collectors return `ValidatedFacts` objects. `as_dict()` exports a copy for
private publication; deserializing that JSON cannot reconstruct a validated
object accepted by `validate_final`. This is an in-process misuse guard, not
authentication against arbitrary Python execution. The finalizer must query AWS
independently: validating worker-supplied snapshots is not an independent read.

Fresh facts have a five-minute limit and cannot predate the supplied fence or
provisional timestamp. Each singleton API fact includes `requestedId`,
`operation`, `observedAt`, raw `response` and positive AWS request metadata.
One request ID cannot stand in for lookups of multiple resources. AccessDenied,
throttling, incomplete metadata and partial results remain unknown. R04 must
verify the fixed account/region and full fresh session discovery without state
filters; these pure contracts cannot authenticate the provenance of JSON.

Registry/observation/fact artifacts use canonical JSON (sorted keys, compact
separators, finite numbers) and exact private S3 versions. Keys below
`automation/v2/staging/`:

| Artifact | Key |
| --- | --- |
| Worker registry | `workers/<sessionId>/registry.json` |
| Inventory | `cleanup/<sessionId>/inventories/<snapshotId>.json` |
| Positive terminal proof | `cleanup/<sessionId>/terminals/<instanceId>.json` |
| Provisional observations/facts | `cleanup/<sessionId>/provisional-compute-observations/<epoch>.json`, `provisional-compute/<epoch>.json` |
| Provisional receipt | `cleanup/<sessionId>/provisional/<epoch>.json` |
| Final observations/facts | `cleanup/<sessionId>/compute-observations/<epoch>.json`, `compute/<epoch>.json` |
| Final workload facts | `cleanup/<sessionId>/workload/<epoch>.json` |
| Final receipt | `cleanup/<sessionId>/final/<epoch>.json` |

Registry rows bind purpose/attempt, launch token/phase, InstanceId, original
volume/ENI IDs and nullable exact `terminalReference`. `PREPARED` cannot invent
compute or terminal proof. Final fact references retain registry, observations,
inventory and state provenance. R05 must restrict terminal/final evidence writes
to the independent finalizer; provisional worker evidence cannot grant final
publication or marker permissions.

## Legacy isolation

`read_legacy` parses a bounded snapshot through the existing v1 validator and
returns a deep copy. It never writes, upgrades, repairs `CALLING`, invents a build
ID, or produces migration/completion evidence. Exact S3 version/hash provenance
must be verified by the migration reader before calling it.

`automation_session.validate` rejects v2 schema/backend values before any v1
lease or transition logic. Historical v1 workflows continue unchanged. The
separate v2 mutation store and protected cutover are not implemented here.

## Control-only migration and cutover

`scripts/migration_v2.py` captures exact raw original session, execution, pointer
and private-input object versions into a `LegacySnapshot`. It checks immutable
scope, matching pointers/inputs and the original dispatch, and exports only a
deep copy of provenance/metadata. It does not upgrade or rewrite the old record.
All original private bytes are kept separately; normalized metadata is not a
replacement for those original objects.

A separate `MigrationExecution` has purpose `legacy-migration`, scope
`control-only`, namespace `migration/control/<migrationId>/execution.json`,
pinned configuration/bundle and the shared reviewed window reference. It is
bound to the captured legacy snapshot. It cannot pass staging execution parsing
or bypass the window's allowed-purpose restrictions. Its record is distinct from
staging admission, allowing R02/R04 to arm a migration recovery owner before
retiring v1 authority. Parsing alone grants no EC2 or EKS permission.

`direct_denial_binding` verifies one exact CloudTrail service denial against the
original dispatch token/environment, project, account/region, caller/issuer,
request/event IDs and timestamps. Its classification is
`EXACT_SERVICE_DENIAL_ONLY`, not proof that no build exists. Redacted token or
environment data cannot satisfy this binding. R02 must additionally verify full
call history, runtime actors and effective authority retirement.

The canonical private manifest at `migration/control/<migrationId>/manifest.json`
binds the exact control execution, snapshot, source revisions, authorization,
review and explicit old-role/trigger retirement inventory. Its policy is either:

- `REQUIRE_DIRECT_DENIAL`: eventual outcome `REJECTED_BEFORE_BUILD_CREATION` only
  after the R02 verifier establishes the direct binding and all other evidence.
- `RETIRE_UNKNOWN_DISPATCH`: eventual outcome
  `UNKNOWN_DISPATCH_AUTHORITY_RETIRED`, under a separately reviewed retirement
  path. It preserves the unresolved dispatch and cannot assert rejected creation.

No force-complete/default fallback is supported. New v2 principals/triggers,
other accounts/regions and broad arbitrary resources are rejected by the manifest
scope checks. R02 must compare its inventory against complete actual service
inventory and the reviewed saved control plan, not assume a format-valid list
proves retirement or completeness.

Closeout is a separate object at
`migration/v1/<legacySessionId>/<URL-encodedExecutionVersion>/closeout.json`.
It retains exact original refs, dispatch ID/phase, manifest and references to all
seven private evidence families: call history, dispatch binding, authority
retirement, actors, workload, recovery-owner health and worker termination.
Neither branch creates a v1 completion marker or changes `CALLING` to `PREPARED`.
`read_closeout` is strictly a wire parser: R02/R06 must verify every referenced
fact, actual human review, all terminal actors, fresh zero inventory/state and
effective policy retirement before publishing/consuming it as admission evidence.

## R02 authenticated collection (in progress)

`scripts/legacy_reconciliation.py collect` owns read-only SDK calls against fixed
AWS endpoints, checks STS account, expected bucket owner, exact object versions
and KMS key, and records private raw responses, request IDs and original object
bytes outside Git. It reads all returned session versions, both project build
histories, StartBuild CloudTrail pages and legacy role/function/rule candidates.
It rechecks current session/pointer/input versions before and after collection.
Pagination errors, access errors, missing metadata and source changes fail the
collection; they never become empty success evidence.

Example (paths must be absolute and private output must be outside every Git
checkout):

```powershell
python scripts/legacy_reconciliation.py collect --profile default `
  --session-id staging-gh-42628903617 `
  --expected-execution-reference C:\private\execution-reference.json `
  --private-output C:\private\techx-evidence
```

The reference file contains the exact original S3 bucket/key/versionId/sha256,
not credentials. Exit 0 means only that collection completed. Every result has
`migrationStatus=NOT_VERIFIED` and `attestationEligible=false`. A collection is
not a closeout and cannot admit staging. Missing/redacted dispatch bindings stay
unresolved; empty build listings do not establish rejected creation. Version
pagination cannot establish that expired/deleted historical versions never
existed. Raw evidence inherits the destination's OS permissions; use the local
private secrets directory with restricted ACLs on Windows.

Remaining R02 work includes complete actor/workload observations, independent
effective authority retirement verification, actual protected human review,
healthy recovery ownership and terminal-worker verification, and trusted
closeout publication/verification. This collector neither disables the old
watchdog nor mutates v1 records, IAM, DNS, workload or compute.

### R02 authority observation collector

`python scripts/legacy_authority.py --profile default --private-output C:\private\techx-evidence`
uses the same fixed-endpoint, account-bound SDK journal. It reads the complete
legacy role roster, trust documents, inline and attached policies at observed
default versions, permissions boundaries, Lambda configuration, EventBridge
rule targets, and schedules in the legacy **techx-staging** group (matching
`bootstrap/automation/cleanup.tf`). Migration manifests use that same group.
New v2 names are excluded, unexpected legacy scopes fail collection. Managed
policy pointers and inline content are read again; changed roles/policies or
rosters fail collection. An absent schedule group is observed through a
successful paginated ListScheduleGroups call; API errors are never emptiness.

The collector reports only narrowly recognized unconditional deny-all structure.
Conditional or partial denies do not count. It does not infer effective IAM
retirement from a small action simulation, nor drain Lambda/SSM/EC2 actors from
policy shape or disabled triggers. Raw policy/configuration data stays private.
Output always says NOT_VERIFIED, actorDrain=NOT_OBSERVED and is ineligible for
attestation. Independent live verification, reviewed mutation, complete actor
inventory/drain and recovery-owner/terminal proof remain required by R02.

### R02 actor observation collector

```powershell
python scripts/legacy_actors.py --profile default `
  --session-id staging-gh-42628903617 `
  --expected-execution-reference C:\private\execution-reference.json `
  --private-output C:\private\techx-evidence
```

It binds original session/execution/pointer/input bytes and rechecks versions,
then reads original-role instance profiles, all returned regional EC2 instance
metadata, SSM managed nodes and paginated command/invocation history from the
original execution time, plus both legacy CodeBuild project histories. Candidate
discovery includes actual legacy role/profile relationships even without a
session tag or expected profile name, the original session tag, shared staging
workload and automation instances without v2 identity. Explicit v2 automation
identity is excluded unless another actual legacy binding still applies.

Stopped EC2 is nonterminal. Recreated profiles, unknown states, access failures,
missing lists, changing role/profile rosters and SSM history races fail collection.
TechX commands targeting absent instances remain unresolved; nonterminal commands
or invocations whose ownership is unscoped remain visible in separate counters.
Raw account-wide metadata and command parameters stay private outside Git.

Every result remains actorDrain=NOT_VERIFIED and attestationEligible=false. EC2
and SSM history expire; empty current listings or offline managed nodes do not
prove historical terminal state. Lambda/GitHub drain, complete original actor
roster, independently verified retirement and migration recovery ownership still
need runtime integration. This CLI is read-only and cannot stop actors or publish
closeout. It adds no operator upload or force-complete route.

## R03 session CAS runtime (in progress)

`scripts/session_runtime_v2.py` is an SDK-backed internal transport for an
already-admitted session, not a bootstrap CLI or an admission/readiness service.
It fetches exact canonical execution and shared-window versions, checks KMS,
expected account/bucket owner and actual STS role identity, then reads
`automation/v2/staging/sessions/<sessionId>.json`. The client must use the fixed
regional endpoints and `Config(retries={"total_max_attempts":1})`; SDK mutation
retries are rejected before any operation.

The runtime record adds `runtimeRevision` and `lastMutationId` to the R01
envelope. `SessionObservation.envelope()` projects that envelope for existing
pure validators. Each read fetches tags for the **exact same VersionId** and
requires all eight body/tag bindings to match: schema, backend, session,
execution hash, status, fence epoch, revision and mutation ID. These are actual
service reads; decoded operator JSON cannot create the observation object.

Preparation restricts transitions by the actual dedicated role and live window;
cleanup may fence the original session after its deadline, while deployment
transitions stop at cleanupStartAt. Cleanup increments the fence epoch. A new
runtime revision prevents same-body ABA within supported state transitions.

Before the conditional session write, commit creates an immutable intent at
`automation/v2/staging/session-mutations/<sessionId>/<mutationId>.json` using
If-None-Match. The semantic ID is independent of the process/principal; a nonce
identifies the winning attempt. Only a positive new-claim response permits one
If-Match session write, with another live identity/window/session check first.
An existing or uncertain claim is observation-only. A process restart cannot
re-prepare an unknown write and regain permission to send it.

Acknowledgement requires positive write metadata/checksum/encryption and exact
read-back body/version/ETag/tags. Lost responses stay uncertain; superseding
fences prevent next-stage acknowledgement. `reconcile(prepared)` and
`recover(mutationId)` are read-only, including outside the deploy window. Recovery
reads the canonical protected claim and its original session version; absence of
the intended head never means rejected creation or permission to replay.

There is no create, arbitrary-key write, expired-lease takeover or completion
API. Bare DESTROYED data is rejected by this reader. R04 must provide the separate
protected finalizer receipt/terminal reader; R06 must verify it before re-admission.

**Not yet enforced in AWS:** R05 must bind allowed status tags to role policies,
require conditional headers, protect create-once claims and immutable/terminal
prefixes, and constrain copy/retag/delete/version-mutation routes. S3 tags are
mutable and are not a signature; existing-object tag conditions cannot prevent
PutObject overwrites. See [AWS tag policy documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/tagging-and-policies.html)
and [tagging permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectTagging.html).
The SDK role check is necessary but does not implement the remaining R03
OIDC/STS/GitHub run/job bridge or complete worker identity/launch binding. A CAS
acknowledgement proves storage state only, never reviewed business acceptance.
Typed SSM orchestration, lease ownership and R04/R06 readiness/finalization guards
still need integration before any v2 cloud admission.

### R03 manager authentication bridge

`manager_bridge_v2.ManagerBridge` owns the STS and fixed-repository GitHub HTTP
reads. Its entry point accepts a raw GitHub OIDC token, an exact execution
reference and a reviewed workflow/job pair. It accepts no caller `verified`
boolean, decoded claims or GitHub context dictionary as authentication.

The first AssumeRoleWithWebIdentity exchange always carries a read-only session
policy. AWS verifies the **same raw JWT**; the bridge checks returned subject,
audience, provider and assumed-role identity, then calls GetCallerIdentity with
the returned credentials. Strict JWT decoding is preflight, never a local
signature verifier. The AWS response supplies the token binding; see the
[STS API](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html).
Credentials/JWTs stay in memory and are excluded from outputs, artifacts and
exceptions generated by this bridge. Existing default-profile credentials are
not used for the exchange or the scoped service clients.

The bootstrap session reads exact S3 execution/window versions. Independent
GitHub requests must match immutable repository/owner IDs, main source SHA,
workflow/event/run/attempt, one live job, its start time and a GitHub-hosted
ubuntu-24.04 runner. Job lists must be complete and bounded to 100; redirects,
pagination, missing request metadata and ambiguous jobs fail closed. Reviewed
workflow/job/environment bindings are explicit in the bridge and must match
the R06 workflow layout. Manager jobs are capped at 45 minutes.

Only after those checks does a second STS exchange issue operational credentials.
Its session policy intersects the R05 base role policy, permits no RunInstances,
and denies new S3/SSM/lifecycle work before the session start or after the earlier
of cleanupStartAt and the original job's 45-minute deadline. Observation/cleanup
request workflows and already-closed windows receive observation credentials;
the only permitted launcher invocation then is the scoped cleanup function.
The policy never replaces the required base IAM/resource/environment controls.

`AuthenticatedManager.require_current()` re-reads live job and AWS/session data.
Its store facade checks again before prepare/commit; replaced/canceled/expired
jobs cannot obtain a new bridge transition. SDK client acquisition also checks
the context. Typed dispatchers still must check mutation admission immediately
before each side effect; an already-issued SDK credential is not automatically
revoked by a GitHub cancellation. Cleanup must remain independent of GitHub.

`as_dict()` is diagnostic only, not a signed remote credential. A lifecycle
launcher **must not authorize from that serialized dictionary**: its receiver
needs its own trusted service/token/caller verification and live fencing. Worker
registration, leases, typed SSM dispatch/result handling and the server-side
handoff are still R03/R04 work; human review, migration and readiness remain R06.
Current v1 workflows are not wired to this bridge and no cloud admission is
enabled by these source changes. The live read-only HTTP probe establishes API
transport only; synthetic SDK tests do not prove live STS/OIDC or effective IAM.

## Verification

Run `python scripts/test-execution-v2.py` from the infra repository. The same
suite runs in `scripts/verify.ps1` and the existing infrastructure CI workflow.
Run `python scripts/test-recovery-v2.py` for checkpoint and result regressions;
it also runs in the existing verification workflow.
`python scripts/test-approval-cleanup-v2.py` covers plan/review, same-worker apply,
cleanup, terminal proof and finalizer misuse; it also runs in infrastructure CI.
`python scripts/test-migration-v2.py` covers shared windows, original snapshots,
control-only scope, redacted service evidence and explicit cutover branches.
See [the R01 coverage map](automation-v2-coverage.md) for contract/runtime scope.
Fixtures are synthetic offline data; they are not cloud execution evidence.
`python scripts/test-legacy-reconciliation.py` exercises the injected SDK call
chain, pagination, private provenance, races, service failures and non-attesting
assessment boundary. It also runs in infrastructure CI.
`python scripts/test-legacy-authority.py` checks SDK policy/trigger collection,
scope, pagination, policy and role races, and conservative deny observations.
`python scripts/test-legacy-actors.py` exercises actual SDK orchestration with
injected clients, broad actor discovery, EC2/SSM unknown/terminal distinctions,
scope, profile races and original history bounds. It runs in infrastructure CI.
`python scripts/test-session-runtime-v2.py` exercises SDK CAS/tag/version checks,
role and clock fencing, durable single-consumption across restarts, conflicts,
lost-response recovery and superseding cleanup; actual SDK service models also
validate the emitted request arguments. It runs in infrastructure CI. These
injected-client tests are not an AWS apply or IAM-policy/live acceptance proof.
`python scripts/test-manager-bridge-v2.py` checks both exchanges, read-only
bootstrap, source/run/job identity, time/cancellation guards, secrets, owned HTTP
transport and actual SDK factories/request shapes. It runs in infrastructure CI.
