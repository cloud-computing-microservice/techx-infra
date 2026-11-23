# R01 contract coverage and boundaries

This is the source-contract milestone, not cloud acceptance or proof that the
replacement runtime is deployed. R02–R12 and G0–G6 remain open. Required CI must
pass on the exact reviewed revision before marking R01 source complete.

| Contract requirement | Implemented validation | Evidence and negative cases | Runtime still required |
| --- | --- | --- | --- |
| V2 scope, sources, budget/window and immutable object | `execution_v2.validate_execution`, `decode_execution`, `bind_authorized_window` | `test-execution-v2.py`, `test-migration-v2.py`: wrong scope/backend/SHA/version, raw-byte checksum, duplicates, bounds, parent-window/purpose changes | R03/R06 trusted reader, actual human grant, live time checks and shared aggregate accounting |
| Session header/epoch | `execution_v2.validate_session` | Wrong execution/version, boolean/zero epoch, legacy schema and extra fields rejected | R03 full CAS lease/stage/checkpoint record, monotonic transitions and fencing |
| GitHub manager identity | `worker_identity.manager_identity` | OIDC/ST​S mismatch, wrong owner/repo/workflow/environment/source, rerun and fake CodeBuild identity | R03/R06 cryptographic OIDC/actual STS and GitHub job/run/history verification |
| Launch and EC2 identity | `validate_launch_intent`, `worker_identity`, `recovery_v2` checkpoint transitions | Wrong config version/token/AMI/profile/network/instance; uncertain CALLING cannot reset; boot/IP cannot silently change | R04 pinned-config lookup, actual API launch/reconciliation, CAS and generation retirement |
| SSM typed task/observation | `worker_identity.validate_handle` | Cross-task/epoch/command/instance/document/stage, arbitrary shell and malformed service/time bindings | R03 exact SendCommand document hash/version, CAS consumption and same-command lost-response recovery |
| Saved plan and human approval | `plan_v2` | Exact plan version even for identical bytes; source/state/config/boot/IP drift, wrong review/rerun/bot/expiry | R03/R06 fresh current context, actual approved API history, action/cost/readiness gates and apply consumption |
| Durable worker result | `recovery_v2.validate_successful_result` | Success/exit-code alone, wrong command/worker/document, missing private version, bad time ordering and booleans | R03/R07/R08 operation-specific business evidence and live commit fencing |
| Workload zero | `cleanup_v2.workload_zero` | Every one of 22 groups must exist and be empty; initialized raw state, no managed instances, correct version/scope/freshness | R04 independent actual inventory/current-state collection and error/pagination handling |
| All session compute and assets | `cleanup_v2.compute_gone` | Unknown/discovered/CALLING/active actors, missing assets, repeated API response, stale facts, surviving public IP/NIC/volume; NotFound needs positive prior termination | R04 complete protected roster, fresh singleton SDK calls, full session discovery, bounded backoff and protected terminal history |
| Provisional versus final | `read_provisional`, `validate_final`, `ValidatedFacts` | Worker/manager cannot seal; JSON cannot replace validated facts; current cleanup owner must be gone; exact fact versions and live epoch required | R04 independent finalizer, private fact publication, conditional DESTROYED/marker writes and failure recovery; R05 IAM exclusivity |
| Original v1 isolation | `execution_v2.read_legacy`, `migration_v2.capture_legacy`; v1 mutation guard | V2 cannot enter legacy mutation API; original raw refs/scope/pointer/input linkage retained; exported data is copied | R02 authenticated complete source/history collection, no legacy repair or fake completion |
| Control-only migration and cutover | `MigrationExecution`, `read_manifest`, `read_closeout`, `direct_denial_binding` | No staging scope/workload overrides, redaction cannot bind direct denial, no force-complete or implicit branch switch, all evidence refs required | R02 trusted producer/verifier and effective retirement; R04 recovery owner and terminal proof; R05/R06 protected review/publishing/admission |

The tests are synthetic offline fixtures; matching service-shaped dictionaries
does not authenticate AWS. Collectors must own the clients and independently
query fixed account/region. Stored research summaries, operator JSON or unit
fixtures cannot be uploaded/accepted as trusted closeout or completion evidence.

Read-only validation on the real legacy input bundle on 22 November preserved
the original `CALLING` snapshot and correctly classified its redacted CloudTrail
event as insufficient direct binding. Original private bytes and S3 history are
stored outside the repository; no v1 state or authority was mutated.

Before R09 source freeze, repeat the **integration** audit against actual v2
entrypoints, IAM and helper pins. These pure contracts intentionally do not prove
the complete end-to-end call graph has migrated away from CodeBuild yet.

## Runtime source increments after R01

The table above records the R01 boundary. Subsequent SDK modules add the
`session_runtime_v2` CAS store, `manager_bridge_v2` owned STS/GitHub verification,
`manager_handoff_v2` independent server verification and `worker_runtime_v2`
worker-owned IMDSv2/STS/EC2 boot identity with private create-once registration.
Their injected SDK tests run in `scripts/verify.ps1`; the main contract documents
each remaining integration/IAM boundary. These modules do not complete R02/R03,
grant admission or establish live AWS acceptance. Worker registry/lease/typed
SSM dispatch, cleanup supervision/finalization and workflow cutover remain open.
