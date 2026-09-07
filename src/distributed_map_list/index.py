"""Builds the JSONL manifest of source/destination prefix pairs consumed by the Distributed Map."""

import json
from datetime import datetime, timedelta

import boto3

s3_client = boto3.client("s3")


def get_dates_in_range(duration, date_format):
    start_date = datetime.now() - timedelta(days=duration)
    return [(start_date + timedelta(days=n)).strftime(date_format) for n in range(duration)]


def ensure_trailing_slash(uri):
    return uri if uri.endswith("/") else uri + "/"


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
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    key = f"locations_{timestamp}.jsonl"

    s3_client.put_object(
        Body=jsonl_content,
        Bucket=bucket_name,
        Key=key,
    )
    print(f"JSONL file uploaded to s3://{bucket_name}/{key}")

    return {
        "s3_locations_bucket": bucket_name,
        "s3_locations_key": key,
    }
