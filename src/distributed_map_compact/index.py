"""Per-prefix compaction invoked by the Distributed Map: merges one prefix's objects into one file."""

import json
import os
import pathlib
import tempfile

import boto3

s3 = boto3.client("s3")

# Encryption header for every write to the target bucket. A bucket default does
# not satisfy a policy that requires the header, so it is explicit when set
TARGET_SSE_ALGORITHM = os.environ.get("TARGET_SSE_ALGORITHM")

# How objects are merged: "concat" appends their bytes, "parquet" rewrites their
# row groups into one Parquet file. Set from the module's file_format variable
FILE_FORMAT = os.environ.get("FILE_FORMAT", "concat")

# Buffered source row groups are written as one output row group once they hold
# this much Arrow data, so a day of tiny files becomes a few large row groups
PARQUET_ROW_GROUP_BYTES = 64 * 1024 * 1024


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


def is_container_format(data):
    """True for formats whose header or footer indexes the whole file, so byte concatenation corrupts them."""
    parquet = data[:4] == b"PAR1" and data[-4:] == b"PAR1"
    avro = data[:4] == b"Obj\x01"
    # ORC opens with the magic and repeats it in the postscript, before its 1-byte length
    orc = data[:3] == b"ORC" and data[-4:-1] == b"ORC"
    return parquet or avro or orc


def concat_objects(source_bucket, objects, out_file):
    for obj in objects:
        data = get_object_from_s3(source_bucket, obj["Key"])
        if is_container_format(data):
            raise ValueError(
                f"s3://{source_bucket}/{obj['Key']} is Parquet, ORC or Avro, which byte concatenation "
                'corrupts. Set file_format = "parquet" for Parquet sources'
            )
        out_file.write(data)


def write_parquet(source_bucket, objects, out_file):
    # pyarrow comes from the Lambda layer attached only when file_format is
    # parquet, so the concat path must not import it
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer = None
    pending, pending_bytes = [], 0
    try:
        for obj in objects:
            uri = f"s3://{source_bucket}/{obj['Key']}"
            try:
                source = pq.ParquetFile(pa.BufferReader(get_object_from_s3(source_bucket, obj["Key"])))
            except pa.ArrowInvalid as err:
                raise ValueError(f"{uri} is not a readable Parquet file: {err}") from err

            if writer is None:
                # Keep the source codec: switching e.g. GZIP to the pyarrow
                # default Snappy would make the compacted copy larger
                codec = "snappy"
                if source.metadata.num_row_groups:
                    codec = source.metadata.row_group(0).column(0).compression
                    codec = {"UNCOMPRESSED": "none", "LZ4_RAW": "lz4"}.get(codec, codec.lower())
                writer = pq.ParquetWriter(out_file, source.schema_arrow, compression=codec)
            elif not source.schema_arrow.equals(writer.schema):
                raise ValueError(
                    f"{uri} has a different schema from the first object under the prefix, "
                    "refusing to merge them into one Parquet file"
                )

            for i in range(source.num_row_groups):
                table = source.read_row_group(i)
                pending.append(table)
                pending_bytes += table.nbytes
                # concat_tables is zero-copy, so the buffer is not held twice
                if pending_bytes >= PARQUET_ROW_GROUP_BYTES:
                    writer.write_table(pa.concat_tables(pending))
                    pending, pending_bytes = [], 0

        if pending:
            writer.write_table(pa.concat_tables(pending))
    finally:
        if writer is not None:
            writer.close()


def merge_objects_from_s3(source_bucket, source_prefix, target_bucket, target_prefix, temp_path):
    objects = list_objects_in_s3(source_bucket, source_prefix)
    if not objects:
        print(f"No objects found under s3://{source_bucket}/{source_prefix}, skipping")
        return

    first_name = objects[0]["Key"].split("/")[-1]
    suffixes = "".join(pathlib.Path(first_name).suffixes)
    out_key = target_prefix + "/" + target_prefix.replace("/", "-") + suffixes

    # A fresh, empty file per run, removed when the block exits even if the
    # merge or upload fails. Lambda reuses warm containers with /tmp intact, so
    # appending to a fixed path would glue this run's data onto whatever a
    # previous or timed-out run left behind, and a long backlog would fill the disk
    with tempfile.NamedTemporaryFile("wb", dir=temp_path, suffix=suffixes, delete_on_close=False) as f:
        if FILE_FORMAT == "parquet":
            write_parquet(source_bucket, objects, f)
        else:
            concat_objects(source_bucket, objects, f)
        # Closed but not yet deleted, so the upload reads the complete file
        f.close()

        # Same call as before when no algorithm is configured; ExtraArgs only
        # when there is something to send
        extra_args = put_args()
        if extra_args:
            s3.upload_file(f.name, target_bucket, out_key, ExtraArgs=extra_args)
        else:
            s3.upload_file(f.name, target_bucket, out_key)

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
