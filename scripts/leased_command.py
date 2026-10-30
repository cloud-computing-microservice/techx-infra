"""Hold and renew the environment CAS lease while a trusted command executes."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time

from automation_session import S3SessionStore, claim, release, transition


def terminate(process):
    if process.poll() is None:
        if os.name == "posix":
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else: process.kill()
            process.wait(timeout=10)


def execute(store, owner, operation, session_id, command, renew_seconds=30, popen=subprocess.Popen, success_status=None, ready_release=None, active_ready=None, rollback_ready=None):
    if not command or not 1 <= renew_seconds <= 120:
        raise ValueError("A command and bounded heartbeat are required")
    if success_status is not None and (success_status != "READY" or not (operation == "deploy" and active_ready is None and rollback_ready is None or operation == "acceptance" and active_ready is not None and rollback_ready is None or operation == "rollback" and rollback_ready is not None)):
        raise ValueError("Only a completed deploy command may hand off READY")
    if ready_release is not None and success_status != "READY":
        raise ValueError("Accepted release payload requires the terminal READY handoff")
    if active_ready is not None and (operation != "acceptance" or success_status != "READY" or ready_release is not None):
        raise ValueError("Active READY requires only its private acceptance callback")
    if rollback_ready is not None and (operation != "rollback" or success_status != "READY" or ready_release is not None or active_ready is not None):
        raise ValueError("Rollback READY requires only its terminal private acceptance callback")
    record, etag = store.read()
    if record["sessionId"] != session_id:
        raise ValueError("Expected session changed")
    owned = claim(record, owner, operation, ttl_seconds=300)
    store.write(owned, etag)
    epoch = owned["lease"]["epoch"]
    process = None
    succeeded = False
    try:
        process = popen(command, start_new_session=(os.name == "posix"))
        while True:
            try:
                code = process.wait(timeout=renew_seconds)
                if code:
                    raise subprocess.CalledProcessError(code, command)
                succeeded = True
                return
            except subprocess.TimeoutExpired:
                current, current_etag = store.read()
                lease = current.get("lease", {})
                if current["sessionId"] != session_id or lease.get("owner") != owner or lease.get("epoch") != epoch:
                    raise ValueError("Lease lost while the command was running")
                store.write(claim(current, owner, operation, ttl_seconds=300), current_etag)
    finally:
        if process is not None:
            terminate(process)
        # Never clear somebody else's lease or overwrite a CAS conflict.
        current, current_etag = store.read()
        if succeeded and success_status:
            lease = current.get("lease", {})
            if current["sessionId"] != session_id or lease.get("owner") != owner or lease.get("epoch") != epoch:
                raise ValueError("Deploy lease lost before terminal handoff")
            try:
                # claim is a validation here, including cleanupStartAt/TTL.
                # Do not publish READY while the child is still executing or
                # leave a deploy heartbeat trying to renew in READY.
                claim(current, owner, operation, ttl_seconds=300)
                accepted = ready_release(current) if ready_release is not None else None
                active = active_ready(current) if active_ready is not None else None
                restored = rollback_ready(current) if rollback_ready is not None else None
                if active_ready is not None and (not isinstance(active, dict) or set(active) != {"acceptedRelease", "acceptedActive"} or current["status"] != "TESTING"):
                    raise ValueError("Active READY payload must contain only the accepted history and baseline")
                if rollback_ready is not None:
                    from rollback_private_contract import validate_private_checkpoint
                    if (not isinstance(restored, dict) or set(restored) != {"acceptedRelease", "acceptedRollback"}
                            or current["status"] != "FAILED_NEEDS_CLEANUP"
                            or tuple(current.get("deploymentTask", {}).get(name) for name in ("mode", "stage", "state")) != ("verify", "rollback", "RUNNING")):
                        raise ValueError("Rollback READY payload requires its exact terminal private task and history")
                    validate_private_checkpoint(current, current["deploymentTask"])
                if ready_release is not None or active_ready is not None or rollback_ready is not None:
                    latest, latest_etag = store.read()
                    if latest != current or latest_etag != current_etag:
                        raise ValueError("Session changed while verifying the accepted READY release")
                    claim(current, owner, operation, ttl_seconds=300)
                # Generic lifecycle transitions cannot turn FAILED into READY.
                # Only this verified terminal private handoff has that edge.
                finished = dict(current, status="READY") if restored is not None else transition(current, owner, success_status)
                if accepted is not None: finished["acceptedRelease"] = accepted
                if active is not None:
                    finished.update(active)
                    finished.pop("releaseCandidate", None)
                    finished.pop("acceptedRollback", None)
                if restored is not None:
                    from accepted_rollback_history import accepted_projection
                    finished.update(restored)
                    finished.pop("releaseCandidate", None)
                    finished.pop("acceptedActive", None)
                    accepted_projection(finished, current["deploymentTask"])
            except ValueError:
                store.write(release(current, owner, epoch), current_etag)
                raise
            store.write(release(finished, owner, epoch), current_etag)
        elif current["sessionId"] == session_id and current.get("lease", {}).get("owner") == owner:
            store.write(release(current, owner, epoch), current_etag)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--operation", choices=("deploy", "release", "bootstrap-publish", "acceptance", "cleanup", "rollback"), required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--success-status", choices=("READY",))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    execute(S3SessionStore(args.bucket), args.owner, args.operation, args.session_id, command, success_status=args.success_status)
