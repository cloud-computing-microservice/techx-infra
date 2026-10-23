# Automation bootstrap

This root is separate from workload state. It defines the private, versioned
S3/KMS backend/control bucket, publisher OIDC role, and the optional independent
cleanup control plane (Lambda launcher, Scheduler group/role, AWS watchdog,
encrypted DLQ and CodeBuild cleanup executor). `enable_cleanup_control` defaults
false. The lifecycle coordinator startup API is implemented, but its deployment
executor, plan approval bridge and GitHub workflow are still being integrated;
do not enable cleanup control or release automation before the complete path is ready.

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
  Set true only after verifying that provider does not exist. If reused, verify
  its audience configuration before applying the role.
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
