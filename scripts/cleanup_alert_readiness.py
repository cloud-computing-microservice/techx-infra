"""Read-only opening gate for confirmed cleanup alerts; not a delivery test."""
import re

ACCOUNT = "058114477594"
TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:techx-automation-cleanup-alerts"
PREFIX = "techx-automation-cleanup-"
METRICS = {
    "launcher-errors": ("AWS/Lambda", "Errors", "Sum", 60, "FunctionName", "techx-automation-cleanup-launcher"),
    "launcher-throttles": ("AWS/Lambda", "Throttles", "Sum", 60, "FunctionName", "techx-automation-cleanup-launcher"),
    "worker-failures": ("AWS/CodeBuild", "FailedBuilds", "Sum", 60, "ProjectName", "techx-automation-cleanup"),
    "delivery-dlq": ("AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", 60, "QueueName", "techx-automation-cleanup-dlq"),
    "deadline-overdue": ("TechX/Automation", "CleanupOverdue", "Maximum", 900, "Environment", "staging"),
}


def verify_cleanup_alerts(clients, kms_key):
    attributes = clients["sns"].get_topic_attributes(TopicArn=TOPIC).get("Attributes", {})
    if (attributes.get("TopicArn") != TOPIC or attributes.get("Owner") != ACCOUNT
            or attributes.get("KmsMasterKeyId") != kms_key
            or attributes.get("SubscriptionsConfirmed") != "1" or attributes.get("SubscriptionsPending") != "0"):
        raise ValueError("Cleanup alerts require the owned encrypted topic and one confirmed operator subscription")
    response = clients["sns"].list_subscriptions_by_topic(TopicArn=TOPIC)
    subscriptions = response.get("Subscriptions")
    if response.get("NextToken") or not isinstance(subscriptions, list) or len(subscriptions) != 1:
        raise ValueError("Cleanup alert subscription inventory is incomplete or differs from the reviewed email route")
    subscription = subscriptions[0]
    if (not isinstance(subscription, dict) or subscription.get("TopicArn") != TOPIC or subscription.get("Owner") != ACCOUNT
            or subscription.get("Protocol") != "email"
            or not isinstance(subscription.get("SubscriptionArn"), str)
            or not re.fullmatch(re.escape(TOPIC) + r":[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", subscription["SubscriptionArn"])
            or not isinstance(subscription.get("Endpoint"), str) or len(subscription["Endpoint"]) > 254
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", subscription["Endpoint"])):
        raise ValueError("Cleanup operator email subscription is pending, foreign or not verified")
    names = sorted(PREFIX + suffix for suffix in METRICS)
    response = clients["cloudwatch"].describe_alarms(AlarmNames=names, AlarmTypes=["MetricAlarm"], MaxRecords=100)
    alarms = response.get("MetricAlarms")
    if (response.get("NextToken") or not isinstance(alarms, list) or len(alarms) != len(names)
            or any(not isinstance(alarm, dict) or not isinstance(alarm.get("AlarmName"), str) for alarm in alarms)
            or sorted(alarm.get("AlarmName", "") for alarm in alarms) != names):
        raise ValueError("All five cleanup alarms must have complete, distinct configuration evidence")
    for alarm in alarms:
        namespace, metric, statistic, period, dimension, value = METRICS[alarm["AlarmName"][len(PREFIX):]]
        expected = dict(AlarmArn=f"arn:aws:cloudwatch:us-east-1:{ACCOUNT}:alarm:{alarm['AlarmName']}",
            ActionsEnabled=True, AlarmActions=[TOPIC], Namespace=namespace, MetricName=metric, Statistic=statistic,
            Period=period, Dimensions=[dict(Name=dimension, Value=value)], Threshold=1,
            ComparisonOperator="GreaterThanOrEqualToThreshold", EvaluationPeriods=1, DatapointsToAlarm=1,
            TreatMissingData="notBreaching")
        wrong_contract = any(alarm.get(key) != item or (key != "Threshold" and type(alarm.get(key)) is not type(item))
                             for key, item in expected.items())
        if (wrong_contract
                or type(alarm.get("Threshold")) not in (int, float)
                or alarm.get("StateValue") not in ("OK", "INSUFFICIENT_DATA")
                or any(alarm.get(key) for key in ("Metrics", "ExtendedStatistic", "OKActions", "InsufficientDataActions"))):
            raise ValueError("Cleanup alarm configuration is disabled, firing or differs from the fixed notification route")
    # Never serialize the operator email or pretend this proves actual delivery.
    return dict(topicArn=TOPIC, subscriptionArn=subscription["SubscriptionArn"], alarmNames=names)
