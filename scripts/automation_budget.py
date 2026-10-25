"""Budget policy shared by lifecycle inputs and session admission.

An alert is not a spending stop. The admission gate is deliberately lower than
the policy ceiling, and the estimate must already cover automation and cleanup.
"""
import math

POLICY_CEILING_USD = 80
AUTOMATIC_APPLY_CEILING_USD = 60


def approved_budget(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= POLICY_CEILING_USD:
        raise ValueError("An explicit finite approved budget between 1 and 80 USD is required")
    return value


def apply_gate(value):
    return min(AUTOMATIC_APPLY_CEILING_USD, approved_budget(value))


def validate_estimate(record, estimate):
    budget = approved_budget(record.get("approvedBudgetUsd"))
    bound = estimate.get("upperBoundUsd")
    estimated_budget = approved_budget(estimate.get("approvedBudgetUsd"))
    hours = record.get("maximumHours")
    if (type(hours) is not int or not 3 <= hours <= 24
            or type(estimate.get("maximumHours")) is not int or estimate["maximumHours"] != hours
            or type(bound) not in (int, float) or not math.isfinite(bound) or not 0 <= bound <= apply_gate(budget)
            or estimate.get("includesAutomation") is not True or estimated_budget != budget):
        raise ValueError("Cost estimate does not cover this approved automation session")
    return budget
