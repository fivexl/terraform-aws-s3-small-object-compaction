"""Standalone compaction: merges the objects of every date prefix in range, one prefix at a time."""

import os
import pathlib
import tempfile
from datetime import datetime, timedelta

import boto3

s3 = boto3.client("s3")

# Encryption header for every write to the target bucket. A bucket default does
# not satisfy a policy that requires the header, so it is explicit when set
TARGET_SSE_ALGORITHM = os.environ.get("TARGET_SSE_ALGORITHM")


def put_args():
    return {"ServerSideEncryption": TARGET_SSE_ALGORITHM} if TARGET_SSE_ALGORITHM else {}


def list_objects_in_s3(bucket, prefix):
    contents = []
    continuation_token = None

    while True:
        if continuation_token:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, ContinuationToken=continuation_token)
        else:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        contents.extend(response.get("Contents", []))

        if "NextContinuationToken" not in response:
            break

        continuation_token = response["NextContinuationToken"]

    return contents


def get_object_from_s3(bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def merge_objects_from_s3(source_bucket, source_prefix, target_bucket, target_prefix, temp_path):
    objects = list_objects_in_s3(source_bucket, source_prefix)
    if not objects:
        print(f"No objects found under s3://{source_bucket}/{source_prefix}, skipping")
        return

    first_name = objects[0]["Key"].split("/")[-1]
    suffixes = "".join(pathlib.Path(first_name).suffixes)
    out_key = target_prefix + "/" + target_prefix.replace("/", "-") + suffixes

    # A fresh, empty file per run that is always removed afterwards. Lambda reuses
    # warm containers with /tmp intact, so appending to a fixed path would glue
    # this run's data onto whatever a previous or timed-out run left behind, and
    # a long backlog would fill the disk
    fd, out_path = tempfile.mkstemp(dir=temp_path, suffix=suffixes)
    try:
        with os.fdopen(fd, "wb") as f:
            for obj in objects:
                f.write(get_object_from_s3(source_bucket, obj["Key"]))

        # Same call as before when no algorithm is configured; ExtraArgs only
        # when there is something to send
        extra_args = put_args()
        if extra_args:
            s3.upload_file(out_path, target_bucket, out_key, ExtraArgs=extra_args)
        else:
            s3.upload_file(out_path, target_bucket, out_key)
    finally:
        os.remove(out_path)

    print(f"Merged {len(objects)} objects into s3://{target_bucket}/{out_key}")


def get_dates_in_range(duration, date_format):
    start_date = datetime.now() - timedelta(days=duration)
    return [(start_date + timedelta(days=n)).strftime(date_format) for n in range(duration)]


def split_s3_parts(s3_uri):
    path_parts = s3_uri.replace("s3://", "").split("/")
    bucket = path_parts.pop(0)
    key = "/".join(path_parts)
    return bucket, key


def handler(event, context):
    print(event)

    source_bucket, source_key = split_s3_parts(event["s3_source_uri"])
    target_bucket, target_key = split_s3_parts(event["s3_destination_uri"])

    dates = get_dates_in_range(event["duration"], event["date_format"])

    for date in dates:
        merge_objects_from_s3(source_bucket, source_key + date, target_bucket, target_key + date, "/tmp/")
        print(f"Compacted {date} of {len(dates)}")

    print("Compaction complete!")
    return {
        "statusCode": 200,
        "body": "Compaction complete!",
    }
