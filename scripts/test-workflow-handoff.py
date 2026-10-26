from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from plan_review_store import checksum
from workflow_handoff import (DispatchUncertain, HandoffDriver, INFRA_REPO, PLATFORM_REPO, guard_child,
                              intent_key, receipt_key, target_for, title, WorkflowAPI)
from wake_lifecycle import wake_existing

spec = importlib.util.spec_from_file_location("workflow_dispatch_support", Path(__file__).with_name("test-lifecycle-dispatch.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = support.DispatchTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.d = self.f.dispatcher
        self.objects, self.runs, self.posts = {}, {}, []
        self.lost_post = self.lost_marker = False
        self.history_count = None
        old_get = self.d.clients["s3"].get_object
        old_put = self.d.clients["s3"].put_object
        def get(**kw):
            if kw["Key"] == "automation/staging/session.json":
                record, _ = self.d.store.read()
                return dict(Body=io.BytesIO(json.dumps(record).encode()), VersionId="current-session")
            if not kw["Key"].startswith("handoffs/"): return old_get(**kw)
            return dict(Body=io.BytesIO(self.objects[kw["Key"]]), VersionId="handoff-v1")
        def put(**kw):
            if not kw["Key"].startswith("handoffs/"): return old_put(**kw)
            self.assertEqual(kw["IfNoneMatch"], "*")
            if kw["Key"] in self.objects:
                raise ClientError(dict(Error=dict(Code="PreconditionFailed")), "PutObject")
            self.objects[kw["Key"]] = kw["Body"]
            if self.lost_marker and kw["Key"].endswith("/calling.json"):
                raise TimeoutError("Call marker response lost")
            return dict(VersionId="handoff-v1")
        self.d.clients["s3"].get_object = get
        self.d.clients["s3"].put_object = put
        self.d.clients["s3"].list_objects_v2 = lambda **kw: dict(IsTruncated=False,
            Contents=[dict(Key=key) for key in self.objects if key.startswith(kw["Prefix"])])
        self.target = target_for("review-plan", self.f.f.record, self.d.execution)
        def dispatch(target, dispatch_id):
            self.posts.append(deepcopy(target))
            number = 300 + len(self.posts)
            self.runs[number] = dict(id=number, head_sha=target["sourceSha"], head_branch="main", event="workflow_dispatch",
                run_attempt=1, path=".github/workflows/" + target["workflow"], display_title=title(dispatch_id),
                status="in_progress", conclusion=None, created_at=self.f.f.record["createdAt"],
                repository=dict(full_name=target["repository"]), head_repository=dict(full_name=target["repository"]))
            if self.lost_post: raise TimeoutError("POST succeeded but response lost")
            return dict(workflow_run_id=number, run_url=f"https://api.github.com/repos/{target['repository']}/actions/runs/{number}",
                        html_url=f"https://github.com/{target['repository']}/actions/runs/{number}")
        def api_get(endpoint):
            if endpoint == "git/ref/heads/main": return dict(object=dict(sha=self.target["sourceSha"]))
            if endpoint.startswith("actions/runs/"): return deepcopy(self.runs[int(endpoint.rsplit("/", 1)[1])])
            if "/runs?" in endpoint:
                return dict(total_count=len(self.runs) if self.history_count is None else self.history_count,
                            workflow_runs=deepcopy(list(self.runs.values())))
            raise AssertionError(endpoint)
        self.api = SimpleNamespace(dispatch=dispatch, get=api_get)
        self.driver = HandoffDriver(self.d, {INFRA_REPO: self.api, PLATFORM_REPO: self.api})

    def child(self, **overrides):
        record, _ = self.d.store.read()
        values = dict(dispatch_id=record["workflowHandoff"]["id"], repository=self.target["repository"], workflow=self.target["workflow"],
            source_sha=self.target["sourceSha"], run_id=301, inputs=deepcopy(self.target["inputs"]), observations=1,
            sleep=lambda _: None, clock=lambda: self.f.f.now)
        values.update(overrides)
        return guard_child(self.d.clients["s3"], self.d.bucket, **values)

    def test_one_post_has_protected_intent_call_marker_receipt_and_verified_child(self):
        result = self.driver.start(self.target)
        self.assertEqual(result["workflow_run_id"], 301)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.objects), 3)
        record, intent, run = self.driver.observe()
        self.assertEqual(record["workflowHandoff"]["phase"], "OBSERVED")
        self.assertEqual(run["id"], 301)
        self.assertEqual(intent["target"], self.target)
        self.child()
        self.assertEqual(self.driver.start(self.target), result)
        self.assertEqual(len(self.posts), 1)

    def test_lost_post_is_recovered_by_real_run_without_another_post(self):
        self.lost_post = True
        result = self.driver.start(self.target)
        self.assertEqual(result["workflow_run_id"], 301)
        self.assertEqual(len(self.posts), 1)
        self.child()

    def test_missing_or_ambiguous_recovery_never_resubmits(self):
        self.lost_marker = True
        with self.assertRaises(DispatchUncertain): self.driver.start(self.target)
        self.assertFalse(self.posts)
        with self.assertRaises(DispatchUncertain): self.driver.start(self.target)
        self.assertFalse(self.posts)
        # Even a corrupted mutable phase cannot reopen the protected call.
        self.f.f.record["workflowHandoff"]["phase"] = "PREPARED"
        with self.assertRaises(DispatchUncertain): self.driver.start(self.target)
        self.assertFalse(self.posts)

    def test_mutable_prepared_pointer_cannot_replay_an_observed_dispatch(self):
        self.driver.start(self.target)
        self.f.f.record["workflowHandoff"]["phase"] = "PREPARED"
        self.driver.start(self.target)
        self.assertEqual(len(self.posts), 1)

    def test_consumed_phase_is_not_downgraded_by_observation(self):
        self.driver.start(self.target)
        self.driver.phase(self.f.f.record["workflowHandoff"]["id"], "CONSUMED")
        self.driver.observe()
        self.assertEqual(self.f.f.record["workflowHandoff"]["phase"], "CONSUMED")

    def test_source_move_before_post_leaves_preparation_without_call(self):
        self.api.get = lambda endpoint: dict(object=dict(sha="0" * 40))
        with self.assertRaisesRegex(ValueError, "main moved"): self.driver.start(self.target)
        self.assertFalse(self.posts)
        self.assertFalse(any(key.endswith("calling.json") for key in self.objects))

    def test_other_operation_cannot_replace_active_handoff(self):
        self.driver.start(self.target)
        other = target_for("cold-bootstrap", self.f.f.record, self.d.execution)
        with self.assertRaisesRegex(ValueError, "not been consumed"): self.driver.start(other)
        self.assertEqual(len(self.posts), 1)

    def test_forged_title_run_attempt_repository_or_source_is_not_positive_evidence(self):
        self.driver.start(self.target)
        for key, value in (("display_title", "other"), ("run_attempt", 2), ("head_sha", "0" * 40),
                           ("repository", dict(full_name="fork/repo")), ("path", ".github/workflows/other.yml")):
            old = self.runs[301][key]
            self.runs[301][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.driver.observe()
            self.runs[301][key] = old

    def test_child_requires_exact_inputs_source_and_run_receipt(self):
        self.driver.start(self.target)
        for fields in (dict(run_id=302), dict(source_sha="0" * 40), dict(inputs=dict(self.target["inputs"], request_sha256="0" * 64))):
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.child(**fields)

    def test_child_without_receipt_or_after_cleanup_never_mutates(self):
        self.driver.start(self.target)
        key = next(key for key in self.objects if key.endswith("receipt.json"))
        del self.objects[key]
        with self.assertRaises(DispatchUncertain): self.child()
        self.f.f.record["status"] = "DESTROYING"
        with self.assertRaisesRegex(ValueError, "closed staging"): self.child()

    def test_null_intent_version_and_changed_protected_bytes_are_rejected(self):
        self.driver.start(self.target)
        record = self.f.f.record
        original = deepcopy(record["workflowHandoff"]["intentReference"])
        record["workflowHandoff"]["intentReference"]["versionId"] = None
        with self.assertRaises(ValueError): self.driver.observe()
        record["workflowHandoff"]["intentReference"] = original
        value = json.loads(self.objects[original["key"]])
        value["target"]["inputs"]["session_id"] = "staging-other"
        self.objects[original["key"]] = json.dumps(value).encode()
        with self.assertRaises(ValueError): self.driver.observe()

    def test_recovery_history_must_be_complete_and_unique(self):
        self.driver.start(self.target)
        key = next(key for key in self.objects if key.endswith("receipt.json"))
        del self.objects[key]
        self.history_count = 101
        with self.assertRaisesRegex(ValueError, "Complete workflow recovery"): self.driver.recover()
        self.history_count = None
        self.runs[302] = dict(self.runs[301], id=302)
        with self.assertRaises(DispatchUncertain): self.driver.recover()
        self.assertEqual(len(self.posts), 1)


class WakeAndWireTests(unittest.TestCase):
    def setUp(self):
        self.run = dict(id=123, event="workflow_dispatch", head_branch="main", head_sha="a" * 40, run_attempt=1,
            path=".github/workflows/bootstrap-staging.yml", status="in_progress",
            repository=dict(full_name=PLATFORM_REPO), head_repository=dict(full_name=PLATFORM_REPO))
        self.sent = []
        self.destination = SimpleNamespace(wake=lambda session, version: (self.sent.append((session, version)) or dict(workflow_run_id=200)))

    def test_platform_callback_forwards_only_existing_session_and_version(self):
        result = wake_existing(SimpleNamespace(get=lambda endpoint: deepcopy(self.run)), self.destination,
            run_id="123", source_sha="a" * 40, session_id="staging-bound", execution_version="-opaque-version")
        self.assertEqual(result["workflow_run_id"], 200)
        self.assertEqual(self.sent, [("staging-bound", "-opaque-version")])

    def test_fork_rerun_or_wrong_workflow_cannot_send_wakeup(self):
        for key, value in (("run_attempt", 2), ("head_sha", "b" * 40), ("head_branch", "feature"),
                           ("path", ".github/workflows/other.yml"), ("repository", dict(full_name="fork/repo"))):
            previous = self.run[key]
            self.run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                wake_existing(SimpleNamespace(get=lambda endpoint: deepcopy(self.run)), self.destination,
                    run_id="123", source_sha="a" * 40, session_id="staging-bound", execution_version="version")
            self.run[key] = previous
        self.assertFalse(self.sent)

    def test_each_infra_child_can_wake_but_opening_or_controller_cannot(self):
        self.run["repository"] = self.run["head_repository"] = dict(full_name=INFRA_REPO)
        for workflow in ("staging-plan-review.yml", "staging-chart-select.yml", "staging-chart-reuse.yml", "staging-chart-promote.yml"):
            self.run["path"] = ".github/workflows/" + workflow
            with self.subTest(workflow=workflow):
                wake_existing(SimpleNamespace(get=lambda endpoint: deepcopy(self.run)), self.destination,
                    run_id="123", source_sha="a" * 40, session_id="staging-bound", execution_version="version", repository=INFRA_REPO)
        self.assertEqual(len(self.sent), 4)
        for workflow in ("staging-lifecycle.yml", "staging-orchestrate.yml", "bootstrap-staging.yml"):
            self.run["path"] = ".github/workflows/" + workflow
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                wake_existing(SimpleNamespace(get=lambda endpoint: deepcopy(self.run)), self.destination,
                    run_id="123", source_sha="a" * 40, session_id="staging-bound", execution_version="version", repository=INFRA_REPO)
        self.assertEqual(len(self.sent), 4)

    def test_api_uses_2026_run_response_without_legacy_post_retry(self):
        api = WorkflowAPI(INFRA_REPO, "fake-test-token")
        response = dict(workflow_run_id=200, run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/200",
                        html_url=f"https://github.com/{INFRA_REPO}/actions/runs/200")
        class HTTP:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return json.dumps(response).encode()
        requests = []
        def http(request, **kwargs):
            requests.append(request)
            return HTTP()
        with patch("workflow_handoff.urlopen", side_effect=http):
            self.assertEqual(api.wake("staging-bound", "-opaque-version"), response)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header("X-github-api-version"), "2026-03-10")
        self.assertEqual(json.loads(requests[0].data), dict(ref="main", inputs=dict(session_id="staging-bound", execution_version="-opaque-version")))
        with patch("workflow_handoff.urlopen", side_effect=TimeoutError("unknown response")) as call:
            with self.assertRaises(TimeoutError): api.wake("staging-bound", "version")
            self.assertEqual(call.call_count, 1)
        with self.assertRaises(ValueError): api.request("actions/workflows/staging-lifecycle.yml/dispatches", {})


if __name__ == "__main__": unittest.main()
