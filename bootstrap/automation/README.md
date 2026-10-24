# Automation bootstrap

This root is separate from workload state. It defines the private, versioned
S3/KMS backend/control bucket, publisher OIDC role, and the optional independent
cleanup control plane (Lambda launcher, Scheduler group/role, AWS watchdog,
encrypted DLQ and CodeBuild cleanup executor). `enable_cleanup_control` defaults
false. The lifecycle coordinator startup API is implemented, but its deployment
executor, plan approval bridge and GitHub workflow are still being integrated;
do not enable cleanup control or release automation before the complete path is ready.

New lifecycle sessions require an explicit `approved_budget_usd` keyword when
calling `initial_record`. `automation_budget.py` validates the finite 1–80 USD
policy and uses `min(60, approvedBudgetUsd)` as the admission gate. The estimate,
immutable execution, session and Terraform private inputs must carry the same
approved amount. Estimates must include automation and match the session window.
The manual cost estimator accepts `-ApprovedBudgetUsd`; its default 80 preserves
the legacy local interface and does not authorize any new deployment. Older
cleanup snapshots without a budget remain readable for deletion recovery.

`scripts/plan_approval.py` supplies the saved-plan approval contract for
foundation/edge: exact S3 object version/checksum, source/config/provider locks,
state lineage/serial, backend, executor role/public `/32`, budget and deadlines.
Context changes or approval expiry require a new plan/review. Initialize and
observe backend state before creating this contract; a fabricated lineage is
not a substitute for cold-start state initialization.

The private bridge is implemented in `scripts/approve_plan.py`,
`scripts/plan_review_store.py` and `.github/workflows/staging-plan-review.yml`.
The planner publishes an immutable request envelope containing sanitized create
addresses/actions and the bounded cost estimate, then CAS-records its exact
key/version/checksum in `session.pendingPlan`. Preview prints only that summary.
After the protected `staging-apply` job starts, the bridge verifies the actual
GitHub review history, exact infra commit/main/workflow/run, current session and
live cleanup schedule before writing `approvals/staging/<session>/...`.
Reruns are rejected: dispatch a new review run so an old environment approval
cannot authorize new inputs. Duplicate writes from the same review preserve the
original approval version/time; different reviewers/runs cannot replace it.

`read_authorized_plan` loads the exact private review, approval and saved-plan
versions for a worker, checking fresh state/backend/network/source/budget context
and the plan bytes before returning them. The bounded lifecycle worker now enforces
deploy-lease ownership, cleanup scheduling, one-time stage consumption and
Terraform state locking. Cold bootstrap and service integration remain incomplete.
No caller-supplied approval JSON is accepted as evidence of a human review.

`enable_plan_approval` defaults false and requires cleanup control. It creates
separate read/approval OIDC roles; neither can change session/state or start a
build. A bucket-policy deny reserves approval writes for the approver role.
Configure `staging-plan-read` and `staging-apply` to allow main only and require
human reviewers on the latter before enabling. An unprotected environment has
no matching approval history and fails closed; unsupported review gates must
not be bypassed. Repository variables are `LIFECYCLE_ENABLED`,
`TECHX_STATE_BUCKET`, `AWS_PLAN_READER_ROLE_ARN`, `AWS_PLAN_APPROVER_ROLE_ARN`.
These settings, OIDC calls and the bridge still require hosted/AWS integration
verification; the lifecycle producer/worker is not complete. Keep all live
automation toggles off until the remaining implementation is ready.

`scripts/lifecycle_stage.py` now implements the producer/consumer stage API:
it uploads a bounded saved plan, publishes its immutable review, CAS-records
`pendingPlan` plus an AWAITING_APPROVAL checkpoint, and consumes the exact
approval once. Foundation must precede edge. An APPLYING checkpoint is durable
before Terraform runs; a missing/failed response becomes UNCERTAIN and
FAILED_NEEDS_CLEANUP, never an automatic replay. A cleanup takeover cannot be
overwritten by an old worker. `apply_saved_plan` runs the existing saved-plan
policy against the actual private JSON before applying that binary plan.

The worker initializes the reviewed remote backend, observes real
state/config/network identity, enforces the toolchain and maintains its
lease. Terraform now defines the gated worker/project/IAM below;
certificate/bootstrap stages and actual service integration still need completion. In particular, resuming a CodeBuild job after
approval must not copy the planner's public IP: differing observed `/32` values
require replan. Use a stable deployment executor across plan/review/apply if
separate CodeBuild executions cannot satisfy this contract.

`leased_command.py --operation deploy --success-status READY` lets the parent
publish READY and release the deploy lease in one CAS only after its child has
terminated successfully. The child must stay PROVISIONING until that handoff;
it cannot publish READY while a deploy heartbeat is still renewing. The parent
rejects handoff after cleanupStartAt or lease loss. Waiting for plan review
does not use this flag and must release the deploy lease without marking READY.

The bridge follows GitHub's [workflow-run review history API](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run).

`terraform test` uses a mocked AWS provider, including one mock apply, and makes
no AWS deployment. `scan.tfvars.example` enables the conditional cleanup resources
for offline security scanning so disabled defaults do not hide findings.

Review and apply this root once with an authorized bootstrap identity. Its state
must be backed up privately; do not store it in Git. `prevent_destroy` protects
the control bucket from workload teardown and accidental bootstrap destroy.
Decommissioning retained state/evidence requires a separate explicit operation.

Inputs:

- `bucket_name`: unique bucket in account `058114477594`.
- `create_github_oidc_provider`: defaults false, reuses the standard provider ARN.
  On initial bootstrap, set true only after verifying that provider does not exist.
  Keep true on subsequent runs when this root's state owns the provider; changing
  to false would plan its deletion. Use false only to reuse a provider outside
  this root's state, and verify its audience before applying the role.
- `cloudflare_secret_arn`: required with cleanup enabled; an existing shared
  `techx/automation/cloudflare-*` Secrets Manager secret whose JSON has `apiToken`
  and `zoneId`. The executor fetches it only when DNS work is actually needed;
  a failure before DNS mutation does not require Cloudflare credentials to remove
  AWS resources. It never deletes this shared automation secret.

After bootstrap, set `TECHX_STATE_BUCKET` and `TECHX_STATE_KMS_KEY_ARN` from
the outputs for staging Terraform execution. The
publisher workflow uses `AWS_PUBLISHER_ROLE_ARN`, `STAGING_SESSION_URI`,
`RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY` in the protected `staging-release`
GitHub Environment. Restrict that Environment to main: the OIDC subject uses the
Environment name, and is not itself a main-branch condition.

The publisher role cannot create ECR repositories or EKS. It can read/CAS-write
only the staging session object and read cleanup schedules; immutable execution
records, the control pointer and cleanup evidence remain outside its S3 scope.
The release workflow uses the pinned `AUTOMATION_INFRA_SHA` helpers to hold a
heartbeat lease across build/publish and chart PR creation. Missing session
objects or invalid/disabled schedules fail closed; OFF does not authorize deploy.

The launcher reads exact S3 execution versions, starts only the fixed
`techx-automation-cleanup` project, reuses a confirmed in-flight cleanup, and
propagates unknown AWS status instead of starting duplicate workers. The periodic
AWS watchdog is independent of GitHub. A successful dispatch is not a successful
teardown: only a separate private inventory completion marker ends retrying.

The cleanup loader reads the exact execution and bundle S3 versions, verifies
checksum and source SHAs, installs checksum-pinned Terraform/Helm/kubectl, then
runs `scripts/run_cleanup.py`. No GitHub credentials are needed. Runtime inputs
are kept in the encrypted `private/staging/<sessionId>/inputs.json` snapshot:
schemaVersion 1, sessionId, validated terraformVars, dnsMutationAttempted and
validationDnsPlans. Lifecycle must persist DNS intents before making the DNS API
mutation. Validation records are removed only after the certificate teardown.

Cleanup fences release-only publishers (which cannot recreate infrastructure),
but requires terminal AWS build status before taking over infrastructure leases.
For persistent deployment EC2 tasks it also waits for every exact-session,
Project=techx/Environment=staging/Role=deployment-executor instance to terminate,
before fencing. This includes idle approval-waiting VMs without a lease; stopping
CodeBuild alone cannot prove that detached SSM child work stopped. Unknown owner,
pagination, missing lease instance or waiter failure refuses takeover. Cleanup
IAM permits termination only for staging private-probe/deployment-executor tags.
It can recover only its own staging-cluster access and one public `/32`, uninstall
Helm while that access still exists, then destroy reviewed deletion-only plans.
Role policies permit no IAM admin, PassRole, ECR repository creation or VPC creation.
SDK access-entry recovery is a scoped control-plane operation, separate from the
deletion-only Terraform plans. Live IAM/service integration remains unverified.

Retention: saved plans expire after one day; evidence after 30 days; state and
session versions remain retained. Customer-managed KMS encryption with rotation,
S3 bucket keys, TLS-only access
and S3 public access blocking are enabled. AWS usage/storage continues outside
the workload session and must be included in the automation cost inventory,
including the retained KMS key. Bucket and key both have prevent_destroy.

### Lifecycle stage runtime (source only)

`scripts/run_lifecycle_stage.py` is the bounded plan/apply entry point for an
immutable-bundle EC2 worker. Its parent holds/renews the deploy lease while the
child runs, then releases it; waiting for human review holds no worker process
or lease. A persistent t3.small keeps its public IPv4 across tasks and is included
separately from the private probe in the cost estimate. Manager/SSM dispatch and
loaders and gated Terraform project/network/roles now exist at source as
detailed below. AMI recipe/manifest packaging is implemented; a real image build,
cold bootstrap and hosted/live integration are still required. This is not a complete deploy command yet.

The worker reads the exact admitted S3 execution version, observes actual EC2
role/instance/AMI/profile/tags/IMDSv2/egress, and verifies the independent schedule
before work. Terraform must use 1.15.5, default workspace, the locked provider
file and the exact staging S3/KMS/account backend. Host Terraform configuration,
credential/endpoint/proxy overrides, provider reattachment and automatic variable
files are rejected. Config hashes include root/module `.tf` and `.tf.json` plus
private variables. A separate task fetches approval from its protected prefix;
it never accepts caller-provided approval JSON. State/config/IP changes reject
apply. Original review expiry survives publish/CAS retries; ambiguous apply is
UNCERTAIN and requires cleanup, not replay.

Admission initializes a truly absent empty workload state under the same
conditional `.tflock` key as Terraform, then deletes only its own lock with
`IfMatch`. Existing lineage is never replaced. A lock conflict, unknown read/write
result or lock replacement refuses admission. Runtime still uses Terraform's
native lock for plan/apply. See the official [Terraform environment reference](https://developer.hashicorp.com/terraform/cli/config/environment-variables),
[S3 backend reference](https://developer.hashicorp.com/terraform/language/backend/s3)
and [S3 conditional DeleteObject contract](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html).

Offline runtime/worker tests cover both stage approvals, fresh-task resume,
missing approvals, changed egress, disabled schedules, immutable publish retry
and lost apply responses. Full infra verify and targeted regressions pass;
IAM/network/AWS behavior is not yet accepted and automation stays disabled.

### Manager, executor and task loaders (source only)

`scripts/run_lifecycle_manager.py` now exposes typed prepare/plan/apply modes.
`deployment_executor.py` verifies the pinned account-owned AMI/bootstrap digest,
exact launch-template version, t3.small with standard CPU credits, 20 GiB encrypted
delete-on-termination root disk, IMDSv2, owned control VPC, no inbound rules,
HTTPS/VPC DNS egress and direct IGW route. Launch intent is stored before
RunInstances with an idempotency client token and exact session tags on instance
and volume. A lost response preserves intent and requests independent cleanup;
it never launches a replacement blindly. Readiness requires Linux SSM online and
agent 3.3.2746.0 or later, which supports ENV_VAR parameter interpolation.

`deployment_tasks.py` stores immutable tasks at `control/staging/tasks/<TaskId>.json`.
The manager publishes the task pointer and releases its lease in one CAS before
SendCommand. The protected document must accept only TaskId, use ENV_VAR with a
32-hex pattern, verify the AMI bootstrap manifest, and invoke the fixed loader.
Its version and SHA256 are pinned, not `$LATEST`/`$DEFAULT`. The EC2 loader downloads
the exact execution/bundle versions and verifies hash/source SHAs before importing
workload code, then claims DISPATCHING → RUNNING once. Duplicate delivery cannot
run another stage. Parent lease heartbeat/release and durable stage result remain
mandatory. Logs go only to the private KMS task prefix; SSM stdout is not evidence
of an applied stage.

`deployment_manager.py` observes the same SSM invocation through eventual
InvocationDoesNotExist responses. It never treats a timeout as terminal or retries
SendCommand. Next dispatch requires SSM Success/exit 0 plus SUCCEEDED task, stage
checkpoint and no lease. `Coordinator.resume_approved()` verifies the trusted
approval and terminal planning task before starting the typed apply build; the
EC2 worker then independently re-observes actual Terraform context. No copied
review context is used as a substitute for live apply observations.

New admissions require versioned `control/staging/executor-config.json`, pinned
inside the immutable execution: schemaVersion, imageId, launchTemplateId/version,
subnetId, securityGroupId, loaderSha256, imageBuildSha256, documentVersion and
documentSha256. `loaderSha256` identifies the reviewed loader manifest;
`imageBuildSha256` covers all normalized installation assets. Both must match
the private AMI tags. Source entry points remain disabled and do not yet
constitute the complete cold-start deployment workflow.

Cleanup reads deploymentLaunch intent. It must observe the exact launch client
token and await VM termination, storing cleanup-owned executor-proof.json before
fencing. The proof survives EC2's terminated-instance metadata eviction. An empty
EC2 response cannot resolve an unknown launch; preserve state/snapshot and report
reconciliation needed rather than writing complete.json. The intent-saved/API-
possibly-not-called reconciliation branch still needs implementation/acceptance.

Tests: manager 14, factory 6, task loader 5, approval resume 5 and runtime 20 pass,
including SDK parameter-shape validation. Full infra verify passes in
`.tmp/cicd-manager-offline.log` in the parent workspace. This is offline evidence,
not proof of deployed roles or working service integration.

Primary API contracts: [SendCommand](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_SendCommand.html),
[GetCommandInvocation](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetCommandInvocation.html),
[SSM parameters](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-syntax-data-elements-parameters.html)
and [agent/interpolation requirements](https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-troubleshooting.html).


### Gated lifecycle infrastructure and executor image

`lifecycle.tf` and `lifecycle-iam.tf` implement the control VPC (172.30.0.0/24),
public executor subnet with direct IGW, no inbound rules, fixed launch template,
typed SSM document, versioned/KMS executor config, non-privileged CodeBuild
manager and environment-bound OIDC dispatcher. The manager can launch only the
reviewed executor and send only the pinned document. The deployment role creates
only staging workload roles with the fixed permissions boundary; it cannot
remove that boundary or change its own IAM policies. Manual staging keeps a null
boundary by default. Cleanup may remove the boundary from its own workload roles
when deleting them.

The control network permits HTTPS egress and VPC DNS only. The HTTPS rule has a
local `AWS-0104` Trivy exception because dynamic AWS endpoints and signed tool
sources are required without NAT. This is not a global scanner exemption.

Two independent flags default false:

- `enable_executor_image_build`: requires an explicit reviewed Canonical Ubuntu
  24.04 amd64 parent AMI. It creates an Image Builder pipeline with no schedule,
  not an image build. A separate authorized operation must start a build. Failed
  build instances terminate automatically. Private AMIs/snapshots persist and
  require a separate retention/decommission decision.
- `enable_lifecycle_control`: requires cleanup, protected plan approval and an
  explicit account-owned executor AMI whose loader/build digests match source.
  It does not make cold bootstrap complete or authorize starting a workload.

The recipe installs Terraform 1.15.5, Helm 4.1.4, kubectl 1.35.0, AWS CLI
2.34.28 and PowerShell 7.6.6. Release hashes are pinned; AWS CLI additionally
requires its official PGP signature and fixed signer fingerprint. Python wheels
use `--require-hashes`. Image tests reject SSM Agent versions below 3.3.2746.0,
missing agents and failed version commands. The disposable Ubuntu verification
workflow has no OIDC or AWS credentials; passing it proves installation only,
not AMI boot, SSM connectivity or deployment IAM.

After a reviewed bootstrap apply in a newly authorized window, export outputs
privately and run resource/context simulations with the permitted dispatcher:

```powershell
terraform -chdir=bootstrap/automation output -json > bootstrap-outputs.private.json
./scripts/check-lifecycle-iam.ps1 -BootstrapOutputsFile bootstrap-outputs.private.json
```

The matrix covers manager launch constraints, workload role boundaries, forbidden
self/approval writes and fixed-project dispatch. API failures, missing context,
incomplete resource results and unexpected decisions reject preflight. This is
simulation evidence only: service behavior, resource policies, SCPs, AMI boot and
SSM networking still require live acceptance. Keep toggles off until all gates
in the workspace CI/CD plan are met; an expired demo authorization cannot fund a
new build/apply. Budget estimates include a build allowance and retained AMI
snapshot allowance, not a spending stop.
