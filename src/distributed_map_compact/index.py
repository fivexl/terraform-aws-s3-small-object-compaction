"""Per-prefix compaction invoked by the Distributed Map: merges one prefix's objects into one file."""

import contextlib
import itertools
import json
import os
import pathlib
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

# How many source objects are fetched at once. Set from the module's
# fetch_concurrency variable; 1 fetches one object at a time
FETCH_CONCURRENCY = max(1, int(os.environ.get("FETCH_CONCURRENCY", "1")))

# Fetched bodies waiting to be written are capped at this many bytes, and at
# twice FETCH_CONCURRENCY objects, so a prefix of large objects does not buffer
# a whole day in memory. One object is always in flight, whatever its size
FETCH_WINDOW_BYTES = 32 * 1024 * 1024

# The merged output is uploaded in parts of this size as it is produced, so
# /tmp holds one part at a time instead of the whole day. Output that never
# fills a part is uploaded as a single object, exactly as a sequential merge
UPLOAD_PART_BYTES = 256 * 1024 * 1024

# S3 rejects a multipart upload whose non-final part is smaller than 5 MiB, so
# a checkpoint never uploads less than this as a part
MIN_PART_BYTES = 5 * 1024 * 1024

# The function stops admitting source objects when less than this is left on
# the invocation clock, uploads the part it has and hands the rest of the date
# to the next invocation of the Map's loop. Enough to upload one part and return
CHECKPOINT_MARGIN_SECONDS = 60

# botocore's default pool of 10 connections would serialise fetches beyond it.
# Concurrent fetches from many Lambdas at once can exceed what one S3 prefix
# serves, and S3 then answers SlowDown; the default retry mode gives up after
# 4 attempts with a short fixed backoff and the whole prefix fails. Adaptive
# mode rate-limits the client when S3 pushes back and keeps retrying, so a
# throttled fetch slows down instead of failing the prefix
S3_RETRY_ATTEMPTS = 10
s3 = boto3.client(
    "s3",
    config=Config(
        max_pool_connections=max(10, FETCH_CONCURRENCY),
        retries={"mode": "adaptive", "max_attempts": S3_RETRY_ATTEMPTS},
    ),
)

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


def list_objects_in_s3(bucket, prefix, start_after=None):
    """Yield the objects under the prefix in key order, a page at a time.

    Lazy so fetching starts on the first page instead of after the last: a
    date of half a million objects is five hundred listing calls. start_after
    resumes the listing after that key, for a continuation
    """
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    if start_after:
        kwargs["StartAfter"] = start_after

    while True:
        response = s3.list_objects_v2(**kwargs)
        yield from response.get("Contents", [])

        if "NextContinuationToken" not in response:
            return

        kwargs["ContinuationToken"] = response["NextContinuationToken"]


def get_object_from_s3(bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def fetch_objects(bucket, objects):
    """Yield (object, body) in listing order, fetching up to FETCH_CONCURRENCY objects at once.

    Fetches run ahead of the writer only while the window holds fewer than
    twice FETCH_CONCURRENCY objects and under FETCH_WINDOW_BYTES, so memory
    stays bounded whatever the object sizes. A failed fetch raises at its turn
    in the order, and the fetches still queued behind it are cancelled.
    """
    if FETCH_CONCURRENCY == 1:
        for obj in objects:
            yield obj, get_object_from_s3(bucket, obj["Key"])
        return

    pending = iter(objects)
    lookahead = next(pending, None)
    window, window_bytes = deque(), 0
    pool = ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY)
    try:
        while lookahead is not None or window:
            # An empty window always admits the next object, so one too large
            # for the byte budget is still fetched, on its own
            while lookahead is not None and (
                not window
                or (
                    len(window) < 2 * FETCH_CONCURRENCY
                    and window_bytes + lookahead.get("Size", 0) <= FETCH_WINDOW_BYTES
                )
            ):
                window.append((lookahead, pool.submit(get_object_from_s3, bucket, lookahead["Key"])))
                window_bytes += lookahead.get("Size", 0)
                lookahead = next(pending, None)

            obj, future = window.popleft()
            data = future.result()
            window_bytes -= obj.get("Size", 0)
            yield obj, data
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


class MultipartSpool:
    """File-like sink that uploads the merged output to S3 in parts as it is written.

    Bytes are spooled to a file under temp_path. Once it holds
    UPLOAD_PART_BYTES it is uploaded as the next part of a multipart upload
    and a fresh file is started, so /tmp holds one part at a time instead of
    the whole output. Output that never fills a part is uploaded with
    upload_file as a single object, as before.

    Writes are whole source objects, so parts end on object boundaries.
    uploaded_through is the key of the last object inside an uploaded part;
    a continuation resumes the listing after it. upload_id and part_number
    come from a continuation when one invocation carries on from another.
    """

    def __init__(self, bucket, key, temp_path, suffixes, upload_id=None, part_number=1):
        self.bucket, self.key = bucket, key
        self.temp_path, self.suffixes = temp_path, suffixes
        self.upload_id, self.created_upload = upload_id, False
        # Number the next uploaded part gets; parts below it are already in S3
        self.part_number = part_number
        self.parts_uploaded = 0
        self.uploaded_through, self.spooled_through = None, None
        self.file, self.file_bytes, self.total_bytes = None, 0, 0
        self.closed = False
        self._open()

    def _open(self):
        # Kept open across writes and closed by the method that uploads or
        # drops it, so no context manager here
        self.file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "wb", dir=self.temp_path, suffix=self.suffixes, delete=False
        )
        self.file_bytes = 0

    def _unlink(self):
        self.file.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.file.name)

    def write(self, data, key=None):
        self.file.write(data)
        self.file_bytes += len(data)
        self.total_bytes += len(data)
        if key is not None:
            self.spooled_through = key
        if self.file_bytes >= UPLOAD_PART_BYTES:
            self.upload_part()
        return len(data)

    # ParquetWriter wraps the sink as a Python file and asks for these
    def flush(self):
        self.file.flush()

    def tell(self):
        return self.total_bytes

    def upload_part(self):
        """Upload the spooled bytes as the next part and start a fresh spool."""
        if self.file_bytes == 0:
            return
        self.file.close()
        try:
            if self.upload_id is None:
                response = s3.create_multipart_upload(Bucket=self.bucket, Key=self.key, **put_args())
                self.upload_id, self.created_upload = response["UploadId"], True
            with open(self.file.name, "rb") as body:
                s3.upload_part(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                    PartNumber=self.part_number,
                    Body=body,
                )
        finally:
            self._unlink()
        self.part_number += 1
        self.parts_uploaded += 1
        self.uploaded_through = self.spooled_through
        self._open()

    def uploaded_parts(self):
        """The parts S3 holds for this upload, numbered below the next one.

        Parts numbered at or above the next one are left by a retried
        invocation that got further before failing; they are not part of this
        attempt's output and S3 drops them when the upload completes without them
        """
        parts, kwargs = [], {"Bucket": self.bucket, "Key": self.key, "UploadId": self.upload_id}
        while True:
            response = s3.list_parts(**kwargs)
            parts.extend(
                {"ETag": part["ETag"], "PartNumber": part["PartNumber"]}
                for part in response.get("Parts", [])
                if part["PartNumber"] < self.part_number
            )
            if not response.get("IsTruncated"):
                return sorted(parts, key=lambda part: part["PartNumber"])
            kwargs["PartNumberMarker"] = response["NextPartNumberMarker"]

    def finish(self):
        """Upload what is spooled and make the object: a single put when no part was ever uploaded."""
        self.closed = True
        if self.upload_id is None:
            self.file.close()
            try:
                # Same call as before when no algorithm is configured;
                # ExtraArgs only when there is something to send
                extra_args = put_args()
                if extra_args:
                    s3.upload_file(self.file.name, self.bucket, self.key, ExtraArgs=extra_args)
                else:
                    s3.upload_file(self.file.name, self.bucket, self.key)
            finally:
                self._unlink()
            return

        # The final part may be any size; an empty spool here means the
        # continuation found nothing left to add
        self.upload_part()
        self._unlink()
        s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.uploaded_parts()},
        )

    def checkpoint(self):
        """Stop here and describe where the next invocation carries on, or None if nothing is in S3 yet.

        A spool big enough to be a part is uploaded. A smaller one is dropped
        and its objects are fetched again next time, since S3 rejects a
        non-final part under MIN_PART_BYTES.
        """
        self.closed = True
        if self.file_bytes >= MIN_PART_BYTES:
            self.upload_part()
        self._unlink()
        if self.upload_id is None:
            return None
        return {
            "upload_id": self.upload_id,
            "key": self.key,
            "start_after": self.uploaded_through,
            "part_number": self.part_number,
        }

    def discard(self):
        """Drop the spool after a failure.

        An upload this invocation created is aborted so no parts linger. One
        inherited from a continuation is left alone: the Map retries the
        failed invocation with the same continuation, which resumes it
        """
        self.closed = True
        self._unlink()
        if self.created_upload:
            s3.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)


def is_container_format(data):
    """True for formats whose header or footer indexes the whole file, so byte concatenation corrupts them."""
    parquet = data[:4] == b"PAR1" and data[-4:] == b"PAR1"
    avro = data[:4] == b"Obj\x01"
    # ORC opens with the magic and repeats it in the postscript, before its 1-byte length
    orc = data[:3] == b"ORC" and data[-4:-1] == b"ORC"
    return parquet or avro or orc


def concat_objects(source_bucket, objects, out_file, out_of_time=lambda: False):
    """Append the objects to out_file in listing order. True if it stopped early because time ran out."""
    fetched = fetch_objects(source_bucket, objects)
    try:
        for obj, data in fetched:
            if is_container_format(data):
                raise ValueError(
                    f"s3://{source_bucket}/{obj['Key']} is Parquet, ORC or Avro, which byte concatenation "
                    'corrupts. Set file_format = "parquet" for Parquet sources'
                )
            out_file.write(data, obj["Key"])
            if out_of_time():
                return True
    finally:
        # Cancels the fetches queued ahead of an early stop
        fetched.close()
    return False


def write_parquet(source_bucket, objects, out_file):
    # pyarrow comes from the Lambda layer attached only when file_format is
    # parquet, so the concat path must not import it
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer = None
    pending, pending_bytes = [], 0
    try:
        for obj, data in fetch_objects(source_bucket, objects):
            uri = f"s3://{source_bucket}/{obj['Key']}"
            try:
                source = pq.ParquetFile(pa.BufferReader(data))
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


def no_progress(source_bucket, source_prefix):
    # A continuation that uploaded nothing would loop forever on the same
    # objects. Fetching 5 MiB cannot take a whole invocation unless the
    # function has too little time or too few concurrent fetches
    raise RuntimeError(
        f"s3://{source_bucket}/{source_prefix}: the invocation ran out of time before it could upload "
        f"a single {MIN_PART_BYTES >> 20} MiB part. Raise compact_lambda_timeout or fetch_concurrency"
    )


def merge_objects_from_s3(
    source_bucket, source_prefix, target_bucket, target_prefix, temp_path, continuation=None, out_of_time=None
):
    """Merge the prefix into one object. Returns a continuation when it ran out of time, else None.

    out_of_time, when given, is polled after every object; once it is true
    the parts uploaded so far stay in S3 and the returned continuation says
    where the next invocation picks up. Only concat can stop and resume: a
    Parquet file is finished by its writer, so parquet mode runs to the end.
    """
    if continuation:
        objects = list_objects_in_s3(source_bucket, source_prefix, continuation["start_after"])
        out_key = continuation["key"]
        suffixes = "".join(pathlib.Path(out_key).suffixes)
        spool_args = {"upload_id": continuation["upload_id"], "part_number": continuation["part_number"]}
    else:
        objects = iter(list_objects_in_s3(source_bucket, source_prefix))
        first = next(objects, None)
        if first is None:
            print(f"No objects found under s3://{source_bucket}/{source_prefix}, skipping")
            return None
        objects = itertools.chain([first], objects)
        suffixes = "".join(pathlib.Path(first["Key"].split("/")[-1]).suffixes)
        out_key = target_prefix + "/" + target_prefix.replace("/", "-") + suffixes
        spool_args = {}

    # A fresh spool per run, removed when the merge ends however it ends.
    # Lambda reuses warm containers with /tmp intact, so appending to a fixed
    # path would glue this run's data onto whatever a previous or timed-out
    # run left behind, and a long backlog would fill the disk
    spool = MultipartSpool(target_bucket, out_key, temp_path, suffixes, **spool_args)
    count = 0

    def counting(objects):
        nonlocal count
        for obj in objects:
            count += 1
            yield obj

    try:
        if FILE_FORMAT == "parquet":
            write_parquet(source_bucket, counting(objects), spool)
        elif concat_objects(source_bucket, counting(objects), spool, out_of_time or (lambda: False)):
            next_step = spool.checkpoint()
            if next_step is None or spool.parts_uploaded == 0:
                no_progress(source_bucket, source_prefix)
            print(
                f"Merged {count} objects of s3://{source_bucket}/{source_prefix} into "
                f"{spool.parts_uploaded} parts, continuing after {next_step['start_after']}"
            )
            return next_step
        spool.finish()
    except BaseException:
        spool.discard()
        raise

    print(f"Merged {count} objects into s3://{target_bucket}/{out_key}")
    return None


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

    def out_of_time():
        return context.get_remaining_time_in_millis() < CHECKPOINT_MARGIN_SECONDS * 1000

    continuation = merge_objects_from_s3(
        source_bucket,
        source_key,
        target_bucket,
        target_key,
        "/tmp/",
        continuation=event.get("continuation"),
        out_of_time=out_of_time,
    )
    if continuation:
        # The Map's item processor loops back into this function while a
        # continuation is present, passing this output as the next input
        return {"src": event["src"], "dest": event["dest"], "continuation": continuation}

    print("Compaction complete!")
    return {
        "statusCode": 200,
        "body": "Compaction complete!",
    }
