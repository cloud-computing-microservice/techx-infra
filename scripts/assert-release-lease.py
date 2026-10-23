import argparse
from datetime import datetime, timezone

from automation_session import S3SessionStore, utc


def verify(record, session_id, owner, now=None):
    now = now or datetime.now(timezone.utc)
    lease = record.get("lease", {})
    if (record["sessionId"] != session_id or record["status"] != "READY"
            or lease.get("owner") != owner or lease.get("operation") != "release"
            or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= now
            or utc(record["cleanupStartAt"]) <= now):
        raise ValueError("The release lease/session is no longer valid")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    record, _ = S3SessionStore(args.bucket).read()
    verify(record, args.session_id, args.owner)
    print("Release lease and staging window are valid.")
