"""Per-prefix compaction invoked by the Distributed Map: merges one prefix's objects into one file."""

import json
import pathlib

import boto3

s3 = boto3.client("s3")


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
    out_path = temp_path + target_prefix.replace("/", "-") + first_name + suffixes
    out_key = target_prefix + "/" + target_prefix.replace("/", "-") + suffixes

    for obj in objects:
        data = get_object_from_s3(source_bucket, obj["Key"])
        with open(out_path, "ab") as f:
            f.write(data)

    s3.upload_file(out_path, target_bucket, out_key)
    print(f"Merged {len(objects)} objects into s3://{target_bucket}/{out_key}")


def split_s3_parts(s3_uri):
    path_parts = s3_uri.replace("s3://", "").split("/")
    bucket = path_parts.pop(0)
    key = "/".join(path_parts)
    return bucket, key


def lambda_handler(event, context):
    print(event)

    # The manifest values are JSON-encoded strings, see distributed_map_list
    source_bucket, source_key = split_s3_parts(json.loads(event["src"]))
    target_bucket, target_key = split_s3_parts(json.loads(event["dest"]))

    merge_objects_from_s3(source_bucket, source_key, target_bucket, target_key, "/tmp/")

    print("Compaction complete!")
    return {
        "statusCode": 200,
        "body": "Compaction complete!",
    }
