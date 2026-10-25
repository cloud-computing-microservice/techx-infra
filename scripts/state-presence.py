"""Distinguish a genuinely missing remote state from AWS access/network failures."""
import argparse
import json
import re


def presence(client, bucket):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket): raise ValueError("Invalid state bucket")
    try:
        response = client.get_object(Bucket=bucket, Key="staging/terraform.tfstate", ExpectedBucketOwner="058114477594")
    except client.exceptions.NoSuchKey:
        return dict(exists=False, managedCount=0)
    state = json.loads(response["Body"].read())
    if state.get("version") != 4 or not isinstance(state.get("resources"), list):
        raise ValueError("Remote state is not a valid Terraform v4 snapshot")
    return dict(exists=True, managedCount=sum(resource.get("mode") == "managed" for resource in state["resources"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    import boto3
    print(json.dumps(presence(boto3.client("s3", region_name="us-east-1"), args.bucket)))
