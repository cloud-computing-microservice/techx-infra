from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from cleanup_alert_readiness import TOPIC, verify_cleanup_alerts


class AlertFixture:
    """SDK-shaped API boundary shared with the real opening-flow regression."""
    def __init__(self, kms):
        self.attributes = dict(TopicArn=TOPIC, Owner="058114477594", KmsMasterKeyId=kms,
            SubscriptionsConfirmed="1", SubscriptionsPending="0")
        self.subscription = dict(SubscriptionArn=TOPIC + ":12345678-1234-1234-1234-123456789abc",
            Owner="058114477594", Protocol="email", TopicArn=TOPIC, Endpoint="operator@example.invalid")
        self.subscriptions = dict(Subscriptions=[self.subscription])
        metrics = [
            ("launcher-errors", "AWS/Lambda", "Errors", "Sum", 60, "FunctionName", "techx-automation-cleanup-launcher"),
            ("launcher-throttles", "AWS/Lambda", "Throttles", "Sum", 60, "FunctionName", "techx-automation-cleanup-launcher"),
            ("worker-failures", "AWS/CodeBuild", "FailedBuilds", "Sum", 60, "ProjectName", "techx-automation-cleanup"),
            ("delivery-dlq", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", 60, "QueueName", "techx-automation-cleanup-dlq"),
            ("deadline-overdue", "TechX/Automation", "CleanupOverdue", "Maximum", 900, "Environment", "staging"),
        ]
        self.alarms = dict(MetricAlarms=[])
        for suffix, namespace, metric, statistic, period, dimension, value in metrics:
            name = "techx-automation-cleanup-" + suffix
            self.alarms["MetricAlarms"].append(dict(AlarmName=name,
                AlarmArn="arn:aws:cloudwatch:us-east-1:058114477594:alarm:" + name,
                ActionsEnabled=True, AlarmActions=[TOPIC], OKActions=[], InsufficientDataActions=[],
                Namespace=namespace, MetricName=metric, Statistic=statistic, Period=period,
                Dimensions=[dict(Name=dimension, Value=value)], Threshold=1.0,
                ComparisonOperator="GreaterThanOrEqualToThreshold", EvaluationPeriods=1, DatapointsToAlarm=1,
                TreatMissingData="notBreaching", StateValue="INSUFFICIENT_DATA"))
        self.calls, self.error = [], None
        session = botocore.session.get_session()
        models = {service: session.get_service_model(service) for service in ("sns", "cloudwatch")}
        def request(service, operation, response, parameters):
            shape = models[service].operation_model(operation)
            validate_parameters(parameters, shape.input_shape)
            self.calls.append((operation, parameters))
            if self.error == operation: raise RuntimeError("Alert API unavailable")
            result = deepcopy(response())
            validate_parameters(result, shape.output_shape)
            return result
        self.clients = dict(sns=SimpleNamespace(
            get_topic_attributes=lambda **kw: request("sns", "GetTopicAttributes", lambda: dict(Attributes=self.attributes), kw),
            list_subscriptions_by_topic=lambda **kw: request("sns", "ListSubscriptionsByTopic", lambda: self.subscriptions, kw)),
            cloudwatch=SimpleNamespace(describe_alarms=lambda **kw: request("cloudwatch", "DescribeAlarms", lambda: self.alarms, kw)))


class AlertReadinessTests(unittest.TestCase):
    def setUp(self):
        self.kms = "arn:aws:kms:us-east-1:058114477594:key/12345678-1234-1234-1234-123456789abc"
        self.f = AlertFixture(self.kms)

    def check(self): return verify_cleanup_alerts(self.f.clients, self.kms)

    def test_confirmed_route_supports_new_alarms_and_emits_no_operator_email(self):
        proof = self.check()
        self.assertEqual(proof["subscriptionArn"], self.f.subscription["SubscriptionArn"])
        self.assertEqual(len(proof["alarmNames"]), 5)
        self.assertNotIn(self.f.subscription["Endpoint"], json.dumps(proof))
        self.assertEqual([name for name, _ in self.f.calls], ["GetTopicAttributes", "ListSubscriptionsByTopic", "DescribeAlarms"])
        request = self.f.calls[-1][1]
        self.assertEqual(request, dict(AlarmNames=proof["alarmNames"], AlarmTypes=["MetricAlarm"], MaxRecords=100))
        for alarm in self.f.alarms["MetricAlarms"]: alarm["StateValue"] = "OK"
        self.assertEqual(self.check(), proof)

    def test_pending_missing_or_foreign_topic_attributes_fail_before_alarm_reads(self):
        for key, value in (("TopicArn", "arn:aws:sns:us-east-1:058114477594:other"), ("Owner", "111111111111"),
                ("KmsMasterKeyId", "alias/aws/sns"), ("SubscriptionsConfirmed", "0"), ("SubscriptionsPending", "1"),
                ("SubscriptionsConfirmed", None)):
            self.f = AlertFixture(self.kms)
            self.f.attributes[key] = value
            # Missing API attributes, rather than an invalid SDK None value.
            if value is None: self.f.attributes.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError): self.check()
            self.assertEqual(len(self.f.calls), 1)

    def test_subscription_must_be_one_owned_confirmed_email_not_another_protocol(self):
        for key, value in (("SubscriptionArn", "PendingConfirmation"), ("Owner", "111111111111"),
                ("Protocol", "https"), ("Endpoint", "not-an-email"), ("TopicArn", TOPIC + "-other")):
            self.f = AlertFixture(self.kms)
            self.f.subscription[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.check()
            self.assertEqual(len(self.f.calls), 2)

    def test_empty_duplicate_or_paginated_subscription_inventory_is_unproven(self):
        for response in ({}, dict(Subscriptions=[]), dict(Subscriptions=[self.f.subscription] * 2),
                dict(Subscriptions=[self.f.subscription], NextToken="more")):
            self.f.subscriptions = response
            with self.subTest(response=response), self.assertRaises(ValueError): self.check()

    def test_missing_duplicate_or_paginated_alarms_cannot_admit_start(self):
        original = deepcopy(self.f.alarms)
        for response in ({}, dict(MetricAlarms=original["MetricAlarms"][:-1]),
                dict(MetricAlarms=original["MetricAlarms"][:-1] + [original["MetricAlarms"][0]]),
                dict(original, NextToken="more")):
            self.f.alarms = response
            with self.subTest(response=response), self.assertRaises(ValueError): self.check()

    def test_disabled_firing_or_drifted_alarm_contract_cannot_admit_start(self):
        for field, value in (("ActionsEnabled", False), ("StateValue", "ALARM"), ("AlarmActions", []),
                ("AlarmArn", "arn:aws:cloudwatch:us-east-1:111111111111:alarm:foreign"), ("MetricName", "Invocations"),
                ("Namespace", "other"), ("Statistic", "Average"), ("Period", 300), ("Threshold", 2.0),
                ("Dimensions", [dict(Name="FunctionName", Value="unrelated")]), ("TreatMissingData", "ignore"),
                ("OKActions", [TOPIC]), ("DatapointsToAlarm", 2)):
            self.f = AlertFixture(self.kms)
            self.f.alarms["MetricAlarms"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.check()

    def test_api_errors_propagate_without_retry_or_any_mutation(self):
        for operation, count in (("GetTopicAttributes", 1), ("ListSubscriptionsByTopic", 2), ("DescribeAlarms", 3)):
            self.f = AlertFixture(self.kms)
            self.f.error = operation
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "unavailable"): self.check()
            self.assertEqual(len(self.f.calls), count)


if __name__ == "__main__": unittest.main()
