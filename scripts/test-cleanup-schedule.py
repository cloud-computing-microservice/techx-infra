from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from cleanup_schedule import register


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.record = dict(account="058114477594", region="us-east-1", environment="staging", status="PLANNING",
                           sessionId="staging-123456", cleanupStartAt=(self.now + timedelta(hours=1)).isoformat(),
                           deadline=(self.now + timedelta(hours=2)).isoformat())
        self.schedule, self.creates = None, []
        class Missing(Exception): pass
        def get_schedule(**kwargs):
            if self.schedule is None: raise Missing()
            return self.schedule
        def create_schedule(**kwargs):
            self.creates.append(kwargs)
            self.schedule = dict(kwargs, Arn="arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/" + kwargs["Name"])
            return dict(ScheduleArn=self.schedule["Arn"])
        self.client = SimpleNamespace(get_schedule=get_schedule, create_schedule=create_schedule,
                                      exceptions=SimpleNamespace(ResourceNotFoundException=Missing))
        self.args = (self.client, self.record, "immutable-execution-v1",
                     "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher",
                     "arn:aws:iam::058114477594:role/techx-automation-scheduler",
                     "arn:aws:sqs:us-east-1:058114477594:techx-automation-cleanup-dlq")

    def test_creates_once_and_verifies_retry(self):
        first = register(*self.args, now=self.now)
        self.assertEqual(first, register(*self.args, now=self.now))
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(self.schedule["FlexibleTimeWindow"], dict(Mode="OFF"))
        self.assertEqual(self.schedule["ScheduleExpressionTimezone"], "UTC")

    def test_never_overwrites_wrong_schedule_or_registers_expired_window(self):
        register(*self.args, now=self.now)
        self.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): register(*self.args, now=self.now)
        self.assertEqual(len(self.creates), 1)
        with self.assertRaises(ValueError): register(*self.args, now=self.now + timedelta(hours=2))


if __name__ == "__main__": unittest.main()
