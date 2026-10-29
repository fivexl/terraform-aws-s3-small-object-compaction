"""Builds the JSONL manifest of source/destination prefix pairs consumed by the Distributed Map."""

import json
import os
from datetime import datetime, timedelta

import boto3

s3_client = boto3.client("s3")

# Encryption header for every write to the target bucket. A bucket default does
# not satisfy a policy that requires the header, so it is explicit when set
TARGET_SSE_ALGORITHM = os.environ.get("TARGET_SSE_ALGORITHM")


def put_args():
    return {"ServerSideEncryption": TARGET_SSE_ALGORITHM} if TARGET_SSE_ALGORITHM else {}


def get_dates_in_range(duration, date_format):
    start_date = datetime.now() - timedelta(days=duration)
    return [(start_date + timedelta(days=n)).strftime(date_format) for n in range(duration)]


def ensure_trailing_slash(uri):
    return uri if uri.endswith("/") else uri + "/"


def manifest_key(now, request_id):
    """Manifest key unique per invocation.

    A key of the timestamp alone is shared by two executions started within
    the same second, so the second manifest overwrites the first and both
    Distributed Maps compact the prefix listed last. The request id of the
    listing invocation tells them apart; the timestamp stays for humans
    """
    return f"locations_{now:%Y%m%d%H%M%S}_{request_id}.jsonl"


def lambda_handler(event, context):
    print(event)

    source_uri = ensure_trailing_slash(event["s3_source_uri"])
    destination_uri = ensure_trailing_slash(event["s3_destination_uri"])

    dates = get_dates_in_range(event["duration"], event["date_format"])
    s3_locations = [
        {
            "src": json.dumps(source_uri + str(date)),
            "dest": json.dumps(destination_uri + str(date)),
        }
        for date in dates
    ]
    print(f"Prefix list complete: {len(s3_locations)} prefixes")

    jsonl_content = "".join(json.dumps(location) + "\n" for location in s3_locations)

    bucket_name = destination_uri.replace("s3://", "").split("/")[0]
    key = manifest_key(datetime.now(), context.aws_request_id)

    s3_client.put_object(
        Body=jsonl_content,
        Bucket=bucket_name,
        Key=key,
        **put_args(),
    )
    print(f"JSONL file uploaded to s3://{bucket_name}/{key}")

    return {
        "s3_locations_bucket": bucket_name,
        "s3_locations_key": key,
    }
