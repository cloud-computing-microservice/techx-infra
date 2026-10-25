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
certificate/PKI assets now run before foundation planning; GitOps bootstrap and actual service integration still need completion. In particular, resuming a CodeBuild job after
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

`scripts/run_lifecycle_manager.py` now exposes typed prepare/plan/apply/bootstrap modes.
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
reconciliation needed rather than writing complete.json. New launch intents use
protocolVersion 1 with PREPARED, then durably write CALLING before invoking EC2.
Cleanup confirms the manager terminal and re-reads the snapshot before deciding:
PREPARED with no executor reference/scoped instance proves no API call and writes
launchNotInvoked evidence. CALLING, legacy intent or a lost checkpoint write
remains uncertain without positive instance termination proof. An empty EC2 read
never resolves those cases. A snapshot read before stopping the manager cannot
justify takeover; fresh instance references must also be observed even without
legacy launch intent. Live failure-injection acceptance remains required.

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

The boundary includes the ENI attribute update required by
[AmazonEKS_CNI_Policy v6](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEKS_CNI_Policy.html).
The mock control-plane test guards this permission within the deployment
account/region ceiling. It does not prove live node/CNI behavior.

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


### Cold-start certificate and secret assets (source only)

Foundation planning now invokes `bootstrap_assets.py` under the deployment lease
and verified cleanup schedule, after backend/toolchain initialization. The fixed
SSM document supplies only the reviewed Cloudflare secret ARN, never its token.
The worker requests the storefront certificate with DNS validation and export
explicitly disabled. Request IAM uses exact domain/region/key/validation/tag
conditions because this action has no resource-level support in the current
[service authorization table](https://docs.aws.amazon.com/service-authorization/latest/reference/list_acm.html).
Existing matching DNS validation records from another owner are reused read-only;
only session-owned validation intents are eligible for cleanup.

VPN PKI is generated with the existing OpenSSL helper in a private temporary
directory outside source. The CA private key is not retained. Server/client keys
and the CA certificate are stored in a session-scoped Secrets Manager secret
with the control KMS key and a fixed version token. No PEM or API key enters
Terraform variables, S3 input snapshots, task output or public artifacts. The
Order API key primitive similarly creates/reuses one immutable secret; its
Kubernetes injection is implemented by the bounded GitOps stage described below; it has not been accepted live.

CreateSecret retries reuse the original current version. A known secret that
vanishes is rejected, never recreated/rotated by retry. PREPARED/CALLING intent
is persisted before generation/API dispatch; an uncertain prior intent cannot
be downgraded after an empty read. Deployment IAM has no PutSecretValue grant.
Both VPN ACM imports include session/role tags and are verified against stored
PEM. ACM SDK retries are disabled because ImportCertificate has no idempotency
token. A lost receipt is recovered by exact session/role discovery; empty or
ambiguous discovery never authorizes another import/request.

`certificate_recovery.py` and `secret_recovery.py` persist exact receipts under
cleanup-owned keys before deletion. Cleanup can delete named secrets even while
ListSecrets has not caught up. After a lost receipt, an empty read does not prove
creation never happened. Cleanup still removes known workload resources but
cannot emit completion when a certificate/secret outcome remains unproven.
A stopped-worker PREPARED secret checkpoint may prove no create call; CALLING
and legacy intents require positive receipts. Keep snapshots and report the
unresolved outcome for reconciliation instead of claiming zero resources.

Local tests cover SDK parameter shapes, actual OpenSSL generation, duplicate
prevention, lost responses, secret immutability, foreign DNS/ownership and
cleanup retry receipts. No AWS certificate/secret API integration is accepted
yet; this does not finish GitOps bootstrap, lifecycle workflows or A12.


### Cold image publication (source only)

`scripts/run_bootstrap_publish.py` is a Docker-capable disposable publisher entry
point, separate from the EC2 Terraform worker. Its protected OIDC identity is
`techx-automation-bootstrap-publisher`, subject to the platform environment
`staging-bootstrap`. Terraform creates it only with lifecycle control enabled;
no new flag/settings have been activated. Existing READY publishers are unchanged.

The new `bootstrap-publish` lease requires PROVISIONING and an APPLIED foundation.
The worker verifies its GitHub run identity, exact execution/source/budget/window,
terminal successful foundation SSM task and live cleanup schedule. It downloads
exact version/hash bundle references through the trusted archive loader, runs
application CI, then extracts those same verified bytes again for image builds.
It rejects linked/generated archived application source; it never reads an
ancestor checkout's Git metadata or adds a skip-clean option to the manual
publisher. Old platform revisions without the reviewed shared publisher core are
rejected rather than silently falling back to another source or entry point.

The platform core preserves existing immutable tags, builds only missing images
and requires completed clean ECR scans. After validating all three digests and the
full source SHA, cold publish stores an immutable version/hash receipt at
`releases/staging/<session>/bootstrap.json` and CAS-records `bootstrapRelease` in
the owned session. CI-generated files and private input snapshots are not image
inputs. Missing source CI, changed scope, cleanup takeover or failed SSM status
cannot publish a successful session reference.

This publisher can push only existing ECR repositories and write its release
receipt/session lease; it cannot create ECR/VPC/EKS/Secrets resources. Like a
normal publisher, cleanup may logically fence its heartbeat while independently
terminating the Terraform executor. Force-removal of workload ECR repositories
prevents a detached upload from recreating them. A stale/closed invocation is
rejected before the early-cleanup failure handler.

The cold-publisher workflow caller, chart proposal/merge coordination and live
integration remain unwired. Protected chart selection and the EC2 GitOps worker
now have source implementations below. A receipt is not a chart merge, running app or
READY evidence. Do not create an Application pointing at moving main or reuse
an old manifest merely because this source primitive exists.


### Protected bootstrap chart selection (source only)

`staging-chart-select.yml` and `select_bootstrap_chart.py` verify the chart before
an Application may consume it. The workflow is manual-dispatch, trusted main,
first attempt, exact admitted infra SHA and `LIFECYCLE_ENABLED` gated. Its separate
`staging-chart-select` environment must restrict deployment branches to main
before activation. Configure `AWS_CHART_SELECTOR_ROLE_ARN` and
`TECHX_STATE_BUCKET`; its short-lived GitHub token needs read access only.

Selection requires an idle PROVISIONING session, APPLIED foundation, exact
version/hash cold-release receipt and live cleanup schedule. It verifies a merged
same-repository chart main PR changing one or both of `values-staging.yaml` and
`releases/staging.json`, plus a human approval of that exact PR head. The PR file
set must equal the full tree diff from the admitted baseline. A later
change request or latest failed/pending main CI cannot reuse an older green run.
GitHub responses must be complete, bounded and from the fixed repository.

The selected full tree may differ from the admitted chart baseline only in the
two release files. Blob Git hashes are checked, the staged values must equal the
approved renderer's output for the cold manifest, and the release JSON must
match all three digests. Permission changes, linked/submodule entries, extra
template changes, a moved main ref or cleanup takeover refuse selection.

An immutable receipt is stored at
`chart-selections/staging/<session>/bootstrap.json`, then CAS-recorded as
`bootstrapChart`. A bucket deny reserves writes/deletes to the selector role;
EC2 has read-only access. The role has no ECR publication, Secret access, deploy,
StartBuild or PassRole authority. A lost session CAS can recover the original
receipt after freshly verifying the same release/PR; an existing selection cannot
be replaced. `read_selection` binds worker consumption to the protected version,
checksum, source/session, release reference and reviewed CI provenance.

The core proof uses GitHub's public REST contracts for
[pull requests](https://docs.github.com/en/rest/pulls/pulls),
[reviews](https://docs.github.com/en/rest/pulls/reviews),
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs) and
[Git trees/blobs](https://docs.github.com/en/rest/git/trees).
This is source/contract verification, not live OIDC, human-review, bucket-policy
or deployment acceptance. The caller/orchestrator, chart proposal/merge gate and
live EC2 GitOps acceptance remain incomplete; do not treat this selection as READY.


### Bounded EC2 GitOps bootstrap (source only)

The manager/SSM loaders accept only `bootstrap --stage gitops --attempt 1` for
this operation. Its immutable task pins the protected `bootstrapChart` reference;
a changed selection, consumed attempt, active edge stage or cleanup takeover
cannot execute. Existing plan/apply actions keep their approval contract. The
updated baked loader changes the AMI loader/build hash: a newly reviewed image
and executor config are required before activating this path.

`run_gitops_bootstrap.py` holds the deploy heartbeat while `gitops_bootstrap.py`
verifies the dedicated EC2 identity, active tagged EKS cluster, regional ARN,
version and exact executor public /32. It builds an isolated private kubeconfig
with the AWS token exec provider and explicit context, discarding ambient
Kubernetes/Helm settings. Every kubectl call has a bounded timeout and rechecks
the task, lease, immutable release, budget and independent cleanup schedule.

After controller readiness, it creates the namespace policy/readiness labels
before pods, and injects the session's immutable Order key from Secrets Manager.
Secret data travels on stdin, with captured output; it is never an argument,
last-applied annotation or task result. Existing objects require session ownership
in an annotation (full session IDs may exceed Kubernetes label length). An
existing Secret must be immutable with the same key; bootstrap never rotates it.
The Application is derived from the admitted baseline, but selects the protected
full chart SHA instead of moving main. It cannot overwrite another session's
Application or a different existing spec.

Completion requires exact Argo revision + Synced/Healthy, all three deployments
and running container digests, ready pods, and true frontend ALB readiness gates.
Cold pods created before a TargetGroupBinding may receive one durably recorded
frontend restart after that binding exists. Missing gates cannot trigger an
unbounded restart loop. The two ingresses must resolve to the same active,
internal, tagged staging ALB in the cluster VPC. Only then is its ARN persisted
privately and a BOOTSTRAPPED checkpoint written. Edge planning/apply refuses
missing readiness or a different private ALB ARN. SSM success alone is insufficient.

The worker does not publish READY. Orchestration, public/private acceptance,
Client VPN end-to-end verification, rollback and cloud integration remain due.
The tests use controlled SDK/kubectl responses, not a running Kubernetes cluster.
See [Kubernetes credential plugins](https://kubernetes.io/docs/reference/access-authn-authz/authentication/#client-go-credential-plugins),
[ALB pod readiness gates](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/deploy/pod_readiness_gate/)
and [Argo CD declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/).

### Existing-session continuation (gated source only)

`staging-continue.yml` accepts only an existing session ID, exact execution
VersionId and an optional request to resume its separately approved plan. It
cannot open a new session, select an arbitrary stage or supply approval itself.
The dedicated dispatcher requires trusted main at the admitted infra SHA, a
first-attempt real GitHub run and the fixed OIDC role/session name. Keep
`LIFECYCLE_ENABLED` false until the remaining lifecycle and live gates pass.
Configure main-only protection for `staging-lifecycle` and
`AWS_LIFECYCLE_DISPATCHER_ROLE_ARN` before any authorized activation.

`lifecycle_dispatch.py` records PREPARED, then CALLING, before StartBuild, and
records its exact build ID as OBSERVED. Tokens alone cannot protect a later
retry: [AWS StartBuild tokens expire after five minutes](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html).
A lost response never permits another dispatch. Subsequent transitions require
the actual project's successful terminal build, expected role/inputs and either
its executor preparation receipt or the exact successful SSM task and durable
stage checkpoint. CAS merges preserve worker fields; cleanup takeover wins.

The continuation observes the same build for a bounded 40-minute loop, retaining
its handle on observation timeout. It can advance preparation to foundation
plan, an approved apply, selected GitOps bootstrap and edge plan. Plan review,
cold release, chart selection and DNS/acceptance are explicit waiting states;
there is no false READY or dispatch to a missing workflow. The separate cold
publish/proposal caller is described below; opening a session and the complete
end-to-end orchestration remain due.

Cleanup stops the observed manager even when no lease/VM is recorded yet. An
unknown StartBuild result requires positive recovery by exact session/version/
operation environment in the fixed project's bounded build history. It checks
the role/project again, confirms terminal status and caches a cleanup-owned
receipt before reading the latest launch snapshot. Empty, ambiguous, incomplete
or truncated observations cannot establish successful cleanup. Pagination omits
sortOrder because [AWS rejects that override for projects with over 100 builds](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_ListBuildsForProject.html).
No new session, cloud build or AWS deployment was executed to test this source.

### Cold publish workflow and chart proposal (gated source only)

Platform's `bootstrap-staging.yml` uses the existing `staging-bootstrap` role,
typed session/execution inputs and a full reviewed `AUTOMATION_INFRA_SHA`. Its
helper checks its own clean Git root/HEAD, the real first-attempt platform main
workflow at the admitted SHA and the latest complete passing `verify.yml` run.
It also verifies the exact preceding lifecycle manager and SSM stage; an active
manager, unknown source/CI or another lease cannot start publication. Source CI
is rechecked during the worker and before receipt publication, rather than
accepting a caller-supplied success flag. Release receipts now require the
`sourceCi` run/source proof; `sourceCiPassed` alone is insufficient.

After publishing, the workflow prepares the chart intent with read-only GitHub
access. A real diff then obtains a chart-scoped App token; exact baseline reuse
records its separate review intent without creating an installation token.
`bootstrap_proposal.py` renders exactly `values-staging.yaml` and
`releases/staging.json` from the admitted archive and cold manifest. Git blob
hashes, the unchanged baseline tree and single admitted parent are checked
before creating `release/bootstrap-<session>`. Commit metadata is deterministic
for safe retries. The API writer exposes only object creation, a new ref and PR
creation; no merge, ref update or delete operation exists. An existing branch
must already identify the exact commit. A lost ref/PR response is reconciled by
positive reads, and a conflicting or closed PR is never replaced/reopened.
This restriction belongs to the trusted entry point; the installation token
still has repository contents/PR write permissions. Main rulesets and promotion
controls are separate activation requirements, not proven by this helper.

The worker holds the bootstrap heartbeat lease and rechecks session, schedule,
source CI, manifest and chart main before every external write. Proposal receipts
at `proposals/staging/<session>/bootstrap.json` are version/hash pinned in the
session; a bucket deny reserves writes for the bootstrap role. Selector reads
that receipt, then requires the actual merged PR head and resulting tree to
match it as well as human review and main CI. Cleanup or a moved main closes
the operation. No PR is merged and neither publisher nor selector marks READY.

Contract tests cover producer-to-selector handoff, different PR/merge SHAs,
GitHub/SSM/CodeBuild failures, latest CI failure, incomplete history, branch
conflicts, lost responses and lost session CAS. These use controlled services;
the GitHub App, protected environments, live OIDC and cloud resources are not
configured/accepted by this source change. Parent lifecycle dispatch, promotion,
DNS/acceptance and rollback remain due. Zero-diff reuse is described below.
REST contracts:
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[Git commits](https://docs.github.com/en/rest/git/commits),
[references](https://docs.github.com/en/rest/git/refs) and
[pull requests](https://docs.github.com/en/rest/pulls/pulls).

### Opening a reviewed session (source only; activation remains disabled)

`staging-lifecycle.yml` has a cloud-free preview, a human-reviewed opening job
and automatic executor/preparation-to-foundation-plan observation. It accepts
two exact peer main SHAs, a 3–24-hour window, a new explicit budget and a private
alert recipient. Infra SHA is the trusted main workflow commit. Every source
needs its latest passing main CI and must still be main at start. The preview
shows the conservative source cost model, backend/KMS, fixed UTC and Vietnam
cleanup/deadline times, and request hash; it omits the email. No private artifact
is uploaded to GitHub. Approval wait consumes the original window and cannot
renew it; whole-second UTC timestamps match the independent scheduler.

`start_lifecycle.py` requires real first-attempt run/approval history for
`staging-start` and the exact dispatcher identity. It rechecks inputs, source CI,
cost assumptions, clean checkouts, S3 versioning/encryption, enabled customer
KMS key, all workload inventory arrays, orphan CloudFront origins/functions,
EKS/VPN logs and recursive/authoritative no-CloudFront DNS proof. Git archive
output is checksum/extract verified against all three SHAs before reservation.
The hosted IP is only the initial snapshot; the EC2 planner substitutes its own
observed stable /32 before generating the separately approved Terraform plan.

Coordinator persists complete execution/private inputs/current pointer before
attempting the cleanup schedule. A lost schedule response retains a failed,
recoverable session and requests cleanup, rather than reopening OFF. No manager
is dispatched without successful schedule readback. The follow-up job can
continue only the immutable execution admitted by that same opening run, and
stops at PLAN_APPROVAL (or a bounded observation limit with the same handle).
It does not imply a completed deployment or automatically approve Terraform.

Required GitHub metadata includes `TECHX_STATE_BUCKET`, `TECHX_KMS_KEY_ARN` and
`AWS_LIFECYCLE_DISPATCHER_ROLE_ARN`. `staging-start` requires a human reviewer;
`staging-lifecycle` is a main-only continuation environment. Source IAM admits
only these two environment subjects and read-only inventory/encryption metadata
in addition to the fixed manager/scheduler controls. The cloud-free CI probe
checks token access to all three source APIs and review history; it does not
prove environment approval, OIDC, AMI/network permissions or AWS acceptance.

With explicit repository-owner authorization, GitHub environments/role metadata
have been prepared, and `AUTOMATION_ENABLED`/`LIFECYCLE_ENABLED` remain false on
all three repositories. AWS resources/roles are not created by GitHub settings.
Independent cleanup can now be requested as described below. Callback
orchestration, promotion/session gates, acceptance/rollback and live integration
remain required before enabling either deployment gate.

### Independent teardown request (source only; not deployed to AWS)

`staging-destroy.yml` takes an existing `session_id` and immutable
`execution_version`. It deliberately remains available when deployment flags are
false or the original deadline has passed. The `staging-destroy` environment
must allow only main; it needs no additional review delay. A dedicated
`AWS_DESTROY_DISPATCHER_ROLE_ARN` can invoke only the fixed AWS cleanup launcher
and read scoped execution, build and inventory evidence. It cannot write state,
start CodeBuild directly, access Cloudflare credentials or delete resources.
The role is defined when cleanup control is enabled, independently of deploy
control. No role is created by repository metadata alone.

The caller must be the actual first-attempt manual workflow on current infra
main. The admitted workload infra commit may be older, so a trusted cleanup fix
can still tear down that exact old session. Current session, execution VersionId
and immutable scope must match before dispatch and during observation. The
launcher and worker retain their own session, ownership and executor-fencing
checks. Neither an expired budget window nor disabling deploy grants permission
to touch a different session.

The helper invokes Lambda once with SDK mutation retries disabled. Lost/error
responses are `DISPATCH_UNCERTAIN`, never an automatic second request or claimed
completion. A running build is observed for up to 40 minutes at the same handle;
`CLEANUP_RUNNING` means inventory has not yet been verified. Successful completion
requires the exact cleanup project/role/session/backend, terminal successful
CodeBuild, a cleanup-owned completion marker, its original inventory bytes/hash,
all 19 empty inventory groups and the matching DESTROYED session checkpoint.
Existing complete evidence is read-only. A DESTROYED checkpoint missing its
marker needs reconciliation; this workflow does not repair or fabricate it.

Repository main protection has also been read back on all three repos: PR and
up-to-date required Actions checks, including administrators, no force/delete
or PR bypass. Chart adds a fresh approval of its last push. These settings do
not implement the still-missing promotion/session gate or prove an installation
token cannot promote after a session closes. No AWS teardown is dispatched by CI
verification, and live cleanup IAM/network/failure acceptance is still required.

### Exact baseline reuse instead of an empty release PR (source only)

Cold preparation renders both allowed files before any GitHub write. If their
Git blobs and modes already equal the admitted complete baseline tree, it writes
a versioned schema-2 `kind: reuse` proposal bound to this session, execution,
release and tree. There is no branch, commit, PR or implicit selection. A real
one-file or two-file diff keeps the reviewed release-PR path. Preparation uses a
read-only GitHub client; only the diff path creates the App token and calls the
writer. Existing normal proposal retries retain the same head/tree contract.

Run `staging-chart-reuse.yml` for the existing session/execution. Its main-only
`staging-chart-read` environment assumes `AWS_CHART_READER_ROLE_ARN` and can only
read the admitted archive/receipts and live cleanup schedule. Preview shows the
exact baseline, three image digests, source SHAs, budget and unchanged deadlines,
without the alert email. The main-only `staging-chart-reuse` job requires a real
human review before assuming the selector role. Its request hash and workflow
run bind that review to the exact preview, not a prior session's merged PR.

After review, the helper recomputes the preview and requires the same source,
manifest, proposal version, tree, current chart main and latest passing CI. It
rechecks workflow/reviewer/CI before writes, and the normal session/deadline/
schedule/CAS guards remain active. The read-only identity cannot publish. A lost
session CAS can recover the same immutable selection receipt; it cannot replace
another release or manufacture a review. Consumers accept this separate schema-2
proof only at the exact admitted baseline. GitOps still checks Synced/Healthy,
all three runtime digests and the shared internal ALB before BOOTSTRAPPED.

Controlled-service tests cover producer → reuse intent → fresh human review →
selection consumption, GitOps bootstrap at that baseline, one-file PR changes,
changed/failed CI, review withdrawal/cancel, source/main/deadline drift, busy
sessions, forged preview/receipt data and lost CAS. These do not prove live OIDC,
review enforcement, GitHub App behavior, runtime or cleanup integration. Parent
callbacks, promotion/session gates, acceptance/rollback and cloud acceptance
remain incomplete; both deployment flags stay false.

### Admitted lifecycle orchestration and protected workflow handoffs (source only)

`staging-orchestrate.yml` observes the existing session after the protected
opening workflow completes. Review, selection and reuse children explicitly
dispatch a wakeup from a final cloud-free reusable job; the platform publisher
does the same with an infra-only App token. This avoids depending on a chain
beyond GitHub's three-level `workflow_run` limit. The controller accepts only
the opening admission or its current protected handoff receipt. Manual wakeups accept only the existing session/execution
version, never a stage, plan, budget or new deployment request. An unfinished
opening actor, unrelated callback, closed session or different source cannot
start successor work. All orchestrator runs share a non-cancelling concurrency
group; this does not replace the S3/service guards.

The controller dispatches the exact private-plan review, consumes its actual
successful run and protected approval, then resumes the exact apply once. It
can dispatch the cold platform workflow, reviewed baseline reuse or selection
of an already merged release, then continue manager/GitOps/edge-plan work.
Observation is bounded across the entire job; its same manager/workflow handle
is retained on timeout. A real failed/cancelled bound workflow requests AWS
cleanup. No callback-provided conclusion or success boolean authorizes apply.

The workflow API uses the 2026-03-10 [dispatch response](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
with a run ID and fixed-repository URLs. Protected S3 intent/calling/receipt
objects precede and bind that call. Only the dispatcher can write their prefix;
plan-reader/approver, publisher and selectors have read-only receipt access.
The session pointer alone cannot reopen a consumed call. A lost POST or receipt
response is recovered only by the protected receipt or one actual first-attempt
main run with the exact dispatch identity; absent, ambiguous or incomplete
history never permits another POST. Children wait for a receipt binding their
own run ID and exact inputs before any mutation. Explicit manual workflows
retain their existing approval/session gates.

Infra's own token needs Actions write for these same-repository dispatches.
Configure `LIFECYCLE_APP_ID`/`LIFECYCLE_APP_PRIVATE_KEY` on infra and platform,
with an installation allowing Actions write on those two repositories; tokens
are minted for only the destination repository. Opening verifies both grants
before AWS reservation. This app is separate from chart proposal contents/PR
write authority. The platform's final notification job uses no AWS credentials
and sends only an existing-session wakeup, including after publish failure when
GitHub schedules that job. The destination observes its own protected child run.
GitHub cancellation/notification delivery still require live failure acceptance;
the independently armed AWS cleanup never depends on a callback succeeding.

Normal chart promotion, chart-main post-merge wakeup, DNS/acceptance, rollback,
App configuration and live OIDC/cleanup integration remain unfinished. A pending
chart PR or its main CI is an explicit wait, not skipped review or a successful
deployment. The cloud-free API probe at run 40180370141 verified the response
contract under the owner's GH account; it did not exercise an installation
token, protected review or AWS. Do not enable either deployment flag yet.
