"""Unit tests for the pure helper functions shared by the Lambda handlers.

The three handler packages each ship an ``index.py``, so they are loaded under
distinct module names via importlib instead of a plain import.
"""

import importlib.util
import io
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# The handlers create a boto3 client at import time, which needs a region but
# no credentials
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
# Every merge test below runs through the thread pool, as a deployment does;
# the sequential path has a test of its own
os.environ.setdefault("FETCH_CONCURRENCY", "4")

SRC_DIR = Path(__file__).parent.parent


def load_handler(module_name, package_dir):
    spec = importlib.util.spec_from_file_location(module_name, SRC_DIR / package_dir / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


standalone = load_handler("standalone_index", "standalone_function_compact")
dm_list = load_handler("dm_list_index", "distributed_map_list")
dm_compact = load_handler("dm_compact_index", "distributed_map_compact")


def test_split_s3_parts():
    assert standalone.split_s3_parts("s3://my-bucket/some/prefix/") == ("my-bucket", "some/prefix/")
    assert dm_compact.split_s3_parts("s3://my-bucket") == ("my-bucket", "")


def test_ensure_trailing_slash():
    assert dm_list.ensure_trailing_slash("s3://bucket/prefix") == "s3://bucket/prefix/"
    assert dm_list.ensure_trailing_slash("s3://bucket/prefix/") == "s3://bucket/prefix/"


def test_manifest_key_differs_between_runs_started_in_the_same_second():
    now = datetime(2026, 10, 28, 18, 9, 7)
    first = dm_list.manifest_key(now, "0a1b2c3d-0000-4000-8000-000000000001")
    second = dm_list.manifest_key(now, "0a1b2c3d-0000-4000-8000-000000000002")
    assert first != second
    assert first == "locations_20261028180907_0a1b2c3d-0000-4000-8000-000000000001.jsonl"


def test_get_dates_in_range_length_and_bounds():
    dates = standalone.get_dates_in_range(3, "%Y/%m/%d")
    assert len(dates) == 3
    # Window is the N days ending yesterday; today is excluded
    assert dates[-1] == (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")
    assert dates[0] == (datetime.now() - timedelta(days=3)).strftime("%Y/%m/%d")


def test_get_dates_in_range_respects_format():
    dates = dm_list.get_dates_in_range(1, "%Y-%m-%d")
    assert dates == [(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]


def test_compact_handlers_retry_s3_throttling_adaptively():
    # A pool of fetching Lambdas can exceed what one S3 prefix serves; S3 then
    # answers SlowDown, and the default retry mode fails the prefix after 4
    # attempts. The clients must back off and keep retrying instead
    for module in (standalone, dm_compact):
        retries = module.s3.meta.config.retries
        assert retries["mode"] == "adaptive"
        # botocore stores max_attempts as the total including the first try
        assert retries["total_max_attempts"] == module.S3_RETRY_ATTEMPTS + 1 == 11


def test_both_compact_handlers_share_merge_logic():
    for module in (standalone, dm_compact):
        assert callable(module.merge_objects_from_s3)
        assert callable(module.list_objects_in_s3)


def _fake_s3(monkeypatch, module, objects, uploads):
    """Stub the S3 calls so merge_objects_from_s3 runs against in-memory data."""
    monkeypatch.setattr(module, "list_objects_in_s3", lambda bucket, prefix: [{"Key": k} for k in objects])
    monkeypatch.setattr(module, "get_object_from_s3", lambda bucket, key: objects[key])

    def upload_file(path, bucket, key):
        uploads.append((key, Path(path).read_bytes()))

    monkeypatch.setattr(module.s3, "upload_file", upload_file)


def test_merge_is_idempotent_on_a_warm_container(monkeypatch, tmp_path):
    """Re-running the same prefix must overwrite, not append, and leave /tmp clean.

    Lambda keeps /tmp between invocations of a warm container, so the merge must
    not depend on the output path being empty when it starts.
    """
    for module in (dm_compact, standalone):
        objects = {"logs/2026/09/01/a.log.gz": b"aaa", "logs/2026/09/01/b.log.gz": b"bbb"}
        uploads = []
        _fake_s3(monkeypatch, module, objects, uploads)
        temp_dir = str(tmp_path / module.__name__) + "/"
        Path(temp_dir).mkdir()

        for _ in range(3):
            module.merge_objects_from_s3("src", "logs/2026/09/01/", "dst", "compacted/2026/09/01", temp_dir)

        assert [content for _, content in uploads] == [b"aaabbb"] * 3
        assert {key for key, _ in uploads} == {"compacted/2026/09/01/compacted-2026-09-01.log.gz"}
        assert list(Path(temp_dir).iterdir()) == []


def test_merge_removes_temp_file_when_upload_fails(monkeypatch, tmp_path):
    objects = {"logs/x/a.log": b"aaa"}
    _fake_s3(monkeypatch, dm_compact, objects, [])

    def failing_upload(path, bucket, key):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(dm_compact.s3, "upload_file", failing_upload)
    temp_dir = str(tmp_path) + "/"

    try:
        dm_compact.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", temp_dir)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the upload failure to propagate")

    assert list(tmp_path.iterdir()) == []


class _Fetches:
    """A get_object stand-in that records how many fetches overlap."""

    def __init__(self, delay_for=lambda key: 0.0, fail_on=None):
        self.in_flight, self.peak, self.started = 0, 0, []
        self.lock = threading.Lock()
        self.delay_for, self.fail_on = delay_for, fail_on

    def __call__(self, bucket, key):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.started.append(key)
        try:
            time.sleep(self.delay_for(key))
            if key == self.fail_on:
                raise RuntimeError(f"fetch of {key} failed")
            return key.encode()
        finally:
            with self.lock:
                self.in_flight -= 1


def test_fetch_objects_keeps_listing_order_while_fetching_concurrently(monkeypatch):
    for module in (dm_compact, standalone):
        monkeypatch.setattr(module, "FETCH_CONCURRENCY", 4)
        objects = [{"Key": f"logs/x/{i:03d}.log", "Size": 1} for i in range(40)]
        # Later objects come back sooner, so listing order can only come from the writer
        fetches = _Fetches(delay_for=lambda key: (40 - int(key[-7:-4])) / 2000)
        monkeypatch.setattr(module, "get_object_from_s3", fetches)

        merged = [data for _, data in module.fetch_objects("src", objects)]

        assert merged == [obj["Key"].encode() for obj in objects]
        assert 1 < fetches.peak <= 4


def test_fetch_objects_bounds_buffered_bytes(monkeypatch):
    """Objects too large to share the byte window are fetched one at a time, not a day's worth at once."""
    monkeypatch.setattr(dm_compact, "FETCH_CONCURRENCY", 8)
    monkeypatch.setattr(dm_compact, "FETCH_WINDOW_BYTES", 100)
    objects = [{"Key": f"logs/x/{i}.log", "Size": 60} for i in range(12)]
    fetches = _Fetches(delay_for=lambda key: 0.01)
    monkeypatch.setattr(dm_compact, "get_object_from_s3", fetches)

    merged = [data for _, data in dm_compact.fetch_objects("src", objects)]

    assert merged == [obj["Key"].encode() for obj in objects]
    assert fetches.peak == 1


def test_fetch_objects_is_sequential_at_concurrency_one(monkeypatch):
    for module in (dm_compact, standalone):
        monkeypatch.setattr(module, "FETCH_CONCURRENCY", 1)
        objects = [{"Key": f"logs/x/{i}.log"} for i in range(6)]
        fetches = _Fetches(delay_for=lambda key: 0.005)
        monkeypatch.setattr(module, "get_object_from_s3", fetches)

        merged = [data for _, data in module.fetch_objects("src", objects)]

        assert merged == [obj["Key"].encode() for obj in objects]
        assert fetches.peak == 1
        assert fetches.started == [obj["Key"] for obj in objects]


def test_merge_fails_on_a_failed_fetch_and_uploads_nothing(monkeypatch, tmp_path):
    for module in (dm_compact, standalone):
        monkeypatch.setattr(module, "FETCH_CONCURRENCY", 4)
        keys = [f"logs/x/{i:02d}.log" for i in range(40)]
        uploads = []
        _fake_s3(monkeypatch, module, dict.fromkeys(keys, b""), uploads)
        fetches = _Fetches(delay_for=lambda key: 0.002, fail_on="logs/x/05.log")
        monkeypatch.setattr(module, "get_object_from_s3", fetches)
        temp_dir = tmp_path / module.__name__
        temp_dir.mkdir()

        with pytest.raises(RuntimeError, match="logs/x/05.log"):
            module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{temp_dir}/")

        assert uploads == []
        assert list(temp_dir.iterdir()) == []
        # The fetches queued behind the failure were cancelled rather than run to the end
        assert len(fetches.started) < len(keys)


def test_put_args_is_empty_without_configured_algorithm(monkeypatch):
    for module in (standalone, dm_list, dm_compact):
        monkeypatch.setattr(module, "TARGET_SSE_ALGORITHM", None)
        assert module.put_args() == {}


def test_put_args_carries_configured_algorithm(monkeypatch):
    for module in (standalone, dm_list, dm_compact):
        monkeypatch.setattr(module, "TARGET_SSE_ALGORITHM", "AES256")
        assert module.put_args() == {"ServerSideEncryption": "AES256"}


def test_merge_sends_encryption_header_when_configured(monkeypatch, tmp_path):
    for module in (dm_compact, standalone):
        objects = {"logs/x/a.log": b"aaa"}
        _fake_s3(monkeypatch, module, objects, [])
        monkeypatch.setattr(module, "TARGET_SSE_ALGORITHM", "AES256")
        calls = []

        def upload_file(path, bucket, key, ExtraArgs=None, calls=calls):
            calls.append(ExtraArgs)

        monkeypatch.setattr(module.s3, "upload_file", upload_file)
        temp_dir = str(tmp_path / module.__name__) + "/"
        Path(temp_dir).mkdir()

        module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", temp_dir)

        assert calls == [{"ServerSideEncryption": "AES256"}]


def _parquet(columns, row_group_size=None, compression="gzip"):
    buf = io.BytesIO()
    pq.write_table(pa.table(columns), buf, row_group_size=row_group_size, compression=compression)
    return buf.getvalue()


def test_is_container_format():
    parquet = _parquet({"a": [1]})
    assert all(module.is_container_format(parquet) for module in (standalone, dm_compact))
    assert standalone.is_container_format(b"Obj\x01rest-of-avro")
    assert standalone.is_container_format(b"ORC...postscript ORC\x17")
    assert not standalone.is_container_format(b"\x1f\x8b\x08 gzip member")
    assert not standalone.is_container_format(b"ORCID,name\n1,a\n")


def test_concat_refuses_parquet_and_uploads_nothing(monkeypatch, tmp_path):
    """Byte concatenation of Parquet writes a file no reader can open, so the run must fail instead."""
    for module in (dm_compact, standalone):
        objects = {"logs/x/a.parquet": _parquet({"a": [1]}), "logs/x/b.parquet": _parquet({"a": [2]})}
        uploads = []
        _fake_s3(monkeypatch, module, objects, uploads)
        monkeypatch.setattr(module, "FILE_FORMAT", "concat")
        temp_dir = tmp_path / module.__name__
        temp_dir.mkdir()

        with pytest.raises(ValueError, match="file_format"):
            module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{temp_dir}/")

        assert uploads == []
        assert list(temp_dir.iterdir()) == []


def test_parquet_merge_keeps_every_row_in_one_row_group(monkeypatch, tmp_path):
    for module in (dm_compact, standalone):
        first = {"srcaddr": ["10.0.0.1", "10.0.0.2"], "bytes": [100, 200]}
        objects = {
            # Two row groups in the first file, coalesced with the second file's one
            "logs/x/00/a.log.parquet": _parquet(first, row_group_size=1),
            "logs/x/01/b.log.parquet": _parquet({"srcaddr": ["10.0.1.1"], "bytes": [300]}),
        }
        uploads = []
        _fake_s3(monkeypatch, module, objects, uploads)
        monkeypatch.setattr(module, "FILE_FORMAT", "parquet")
        temp_dir = tmp_path / module.__name__
        temp_dir.mkdir()

        module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{temp_dir}/")

        [(key, content)] = uploads
        assert key == "compacted/x/compacted-x.log.parquet"
        merged = pq.ParquetFile(io.BytesIO(content))
        assert merged.read().to_pydict() == {
            "srcaddr": ["10.0.0.1", "10.0.0.2", "10.0.1.1"],
            "bytes": [100, 200, 300],
        }
        assert merged.metadata.num_row_groups == 1
        # The source codec is kept rather than replaced by the pyarrow default
        assert merged.metadata.row_group(0).column(0).compression == "GZIP"
        assert list(temp_dir.iterdir()) == []


def test_parquet_merge_refuses_mixed_schemas(monkeypatch, tmp_path):
    objects = {
        "logs/x/a.parquet": _parquet({"bytes": [1]}),
        "logs/x/b.parquet": _parquet({"bytes": ["one"]}),
    }
    uploads = []
    _fake_s3(monkeypatch, dm_compact, objects, uploads)
    monkeypatch.setattr(dm_compact, "FILE_FORMAT", "parquet")

    with pytest.raises(ValueError, match="different schema"):
        dm_compact.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/")

    assert uploads == []
    assert list(tmp_path.iterdir()) == []


def test_parquet_merge_refuses_a_non_parquet_object(monkeypatch, tmp_path):
    objects = {"logs/x/a.parquet": _parquet({"bytes": [1]}), "logs/x/_SUCCESS": b""}
    uploads = []
    _fake_s3(monkeypatch, standalone, objects, uploads)
    monkeypatch.setattr(standalone, "FILE_FORMAT", "parquet")

    with pytest.raises(ValueError, match="_SUCCESS is not a readable Parquet file"):
        standalone.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/")

    assert uploads == []


class _MultipartS3:
    """An S3 stand-in for the multipart calls the spool makes, keeping parts per upload."""

    def __init__(self):
        self.uploads, self.completed, self.aborted, self.single_puts = {}, [], [], []
        self.next_id = 0

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        self.next_id += 1
        upload_id = f"upload-{self.next_id}"
        self.uploads[upload_id] = {"key": Key, "parts": {}, "args": kwargs}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.uploads[UploadId]["parts"][PartNumber] = Body.read()
        return {"ETag": f"etag-{PartNumber}"}

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker=0):
        # One part per page, so the pagination is exercised
        numbers = sorted(n for n in self.uploads[UploadId]["parts"] if n > PartNumberMarker)
        page = numbers[:1]
        return {
            "Parts": [{"PartNumber": n, "ETag": f"etag-{n}"} for n in page],
            "IsTruncated": len(numbers) > 1,
            "NextPartNumberMarker": page[0] if page else PartNumberMarker,
        }

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        upload = self.uploads.pop(UploadId)
        content = b"".join(upload["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"])
        self.completed.append((Key, content, [p["PartNumber"] for p in MultipartUpload["Parts"]]))

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId)

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.single_puts.append((key, Path(path).read_bytes()))


def _fake_multipart_s3(monkeypatch, module, objects, part_bytes=10, min_part_bytes=5):
    """Stub the S3 calls with tiny part sizes so a few bytes of input go multipart."""
    fake = _MultipartS3()
    for name in ("create_multipart_upload", "upload_part", "list_parts", "complete_multipart_upload"):
        monkeypatch.setattr(module.s3, name, getattr(fake, name))
    monkeypatch.setattr(module.s3, "abort_multipart_upload", fake.abort_multipart_upload)
    monkeypatch.setattr(module.s3, "upload_file", fake.upload_file)
    monkeypatch.setattr(module, "UPLOAD_PART_BYTES", part_bytes)
    monkeypatch.setattr(module, "MIN_PART_BYTES", min_part_bytes)
    keys = sorted(objects)

    def list_objects(bucket, prefix, start_after=None):
        return [{"Key": k, "Size": len(objects[k])} for k in keys if start_after is None or k > start_after]

    monkeypatch.setattr(module, "list_objects_in_s3", list_objects)
    monkeypatch.setattr(module, "get_object_from_s3", lambda bucket, key: objects[key])
    return fake


def test_merge_uploads_in_parts_once_the_output_exceeds_one_part(monkeypatch, tmp_path):
    """A day larger than /tmp streams out as parts, byte-identical to the single-file merge."""
    for module in (dm_compact, standalone):
        objects = {f"logs/x/{i:02d}.log": f"{i:02d}-".encode() for i in range(12)}
        fake = _fake_multipart_s3(monkeypatch, module, objects, part_bytes=10)
        temp_dir = tmp_path / module.__name__
        temp_dir.mkdir()

        module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{temp_dir}/")

        [(key, content, numbers)] = fake.completed
        assert key == "compacted/x/compacted-x.log"
        assert content == b"".join(objects[k] for k in sorted(objects))
        # 36 bytes in: three full 12-byte parts (parts close on an object boundary past 10 bytes)
        assert numbers == [1, 2, 3]
        assert fake.single_puts == []
        assert fake.uploads == {}
        assert list(temp_dir.iterdir()) == []


def test_small_output_is_still_a_single_put(monkeypatch, tmp_path):
    for module in (dm_compact, standalone):
        objects = {"logs/x/a.log": b"aaa", "logs/x/b.log": b"bbb"}
        fake = _fake_multipart_s3(monkeypatch, module, objects, part_bytes=100)

        module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/")

        assert fake.single_puts == [("compacted/x/compacted-x.log", b"aaabbb")]
        assert fake.completed == [] and fake.uploads == {}


def test_merge_aborts_its_own_multipart_upload_when_a_fetch_fails(monkeypatch, tmp_path):
    for module in (dm_compact, standalone):
        monkeypatch.setattr(module, "FETCH_CONCURRENCY", 4)
        objects = {f"logs/x/{i:02d}.log": b"0123456789" for i in range(20)}
        fake = _fake_multipart_s3(monkeypatch, module, objects, part_bytes=10)
        monkeypatch.setattr(module, "get_object_from_s3", _Fetches(fail_on="logs/x/05.log"))
        temp_dir = tmp_path / module.__name__
        temp_dir.mkdir()

        with pytest.raises(RuntimeError, match="logs/x/05.log"):
            module.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{temp_dir}/")

        assert fake.aborted == ["upload-1"]
        assert fake.completed == [] and fake.uploads == {}
        assert list(temp_dir.iterdir()) == []


def test_parquet_merge_streams_through_the_spool(monkeypatch, tmp_path):
    objects = {f"logs/x/{i:02d}.parquet": _parquet({"n": [i] * 50}) for i in range(4)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=300)
    monkeypatch.setattr(dm_compact, "FILE_FORMAT", "parquet")

    dm_compact.merge_objects_from_s3("src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/")

    [(key, content, numbers)] = fake.completed
    assert key == "compacted/x/compacted-x.parquet"
    assert len(numbers) > 1
    assert pq.read_table(io.BytesIO(content)).to_pydict() == {"n": [i for i in range(4) for _ in range(50)]}
    assert list(tmp_path.iterdir()) == []


def _out_of_time_after(objects_written, fetches):
    """An out_of_time stand-in that fires once that many objects have been fetched."""

    def out_of_time():
        return len(fetches.started) >= objects_written

    return out_of_time


def test_merge_checkpoints_when_out_of_time_and_a_continuation_finishes_the_day(monkeypatch, tmp_path):
    """Parts uploaded before the clock ran out stay; the next invocation resumes after them."""
    monkeypatch.setattr(dm_compact, "FETCH_CONCURRENCY", 1)
    objects = {f"logs/x/{i:02d}.log": f"{i:02d}-".encode() for i in range(12)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=10, min_part_bytes=5)
    fetches = _Fetches()
    monkeypatch.setattr(
        dm_compact, "get_object_from_s3", lambda bucket, key: objects[fetches(bucket, key).decode()]
    )

    continuation = dm_compact.merge_objects_from_s3(
        "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", out_of_time=_out_of_time_after(5, fetches)
    )

    # Objects 00-03 made a 12-byte part; 04 alone (3 bytes) is under the 5-byte
    # minimum, so it is dropped and refetched rather than uploaded short
    assert continuation == {
        "upload_id": "upload-1",
        "key": "compacted/x/compacted-x.log",
        "start_after": "logs/x/03.log",
        "part_number": 2,
    }
    assert sorted(fake.uploads["upload-1"]["parts"]) == [1]
    assert list(tmp_path.iterdir()) == []

    assert (
        dm_compact.merge_objects_from_s3(
            "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", continuation=continuation
        )
        is None
    )

    [(key, content, numbers)] = fake.completed
    assert key == "compacted/x/compacted-x.log"
    assert content == b"".join(objects[k] for k in sorted(objects))
    assert numbers == [1, 2, 3]
    assert fake.uploads == {} and fake.single_puts == []
    assert list(tmp_path.iterdir()) == []


def test_checkpoint_uploads_a_spool_that_is_big_enough_to_be_a_part(monkeypatch, tmp_path):
    monkeypatch.setattr(dm_compact, "FETCH_CONCURRENCY", 1)
    objects = {f"logs/x/{i:02d}.log": f"{i:02d}-".encode() for i in range(12)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=10, min_part_bytes=5)
    fetches = _Fetches()
    monkeypatch.setattr(
        dm_compact, "get_object_from_s3", lambda bucket, key: objects[fetches(bucket, key).decode()]
    )

    continuation = dm_compact.merge_objects_from_s3(
        "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", out_of_time=_out_of_time_after(6, fetches)
    )

    # 04 and 05 are 6 bytes: a legal part, so they are kept
    assert continuation["start_after"] == "logs/x/05.log"
    assert continuation["part_number"] == 3
    assert fake.uploads["upload-1"]["parts"][2] == b"04-05-"


def test_merge_fails_instead_of_looping_when_no_part_was_uploaded_in_time(monkeypatch, tmp_path):
    objects = {"logs/x/a.log": b"aaa", "logs/x/b.log": b"bbb"}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=100, min_part_bytes=50)

    with pytest.raises(RuntimeError, match="compact_lambda_timeout"):
        dm_compact.merge_objects_from_s3(
            "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", out_of_time=lambda: True
        )

    assert fake.uploads == {} and fake.completed == [] and fake.single_puts == []
    assert list(tmp_path.iterdir()) == []


def test_continuation_completes_without_stale_parts_from_a_retried_invocation(monkeypatch, tmp_path):
    """A retried continuation overwrites its part numbers; higher ones from the failed attempt stay out."""
    objects = {f"logs/x/{i:02d}.log": f"{i:02d}-".encode() for i in range(8)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=10)
    upload_id = fake.create_multipart_upload("dst", "compacted/x/compacted-x.log")["UploadId"]
    fake.uploads[upload_id]["parts"] = {1: b"00-01-02-03-", 2: b"STALE", 3: b"STALE"}
    continuation = {
        "upload_id": upload_id,
        "key": "compacted/x/compacted-x.log",
        "start_after": "logs/x/03.log",
        "part_number": 2,
    }

    dm_compact.merge_objects_from_s3(
        "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", continuation=continuation
    )

    [(_, content, numbers)] = fake.completed
    assert content == b"".join(objects[k] for k in sorted(objects))
    assert numbers == [1, 2]


def test_continuation_failure_leaves_the_inherited_upload_for_the_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(dm_compact, "FETCH_CONCURRENCY", 4)
    objects = {f"logs/x/{i:02d}.log": b"0123456789" for i in range(20)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=10)
    upload_id = fake.create_multipart_upload("dst", "compacted/x/compacted-x.log")["UploadId"]
    fake.uploads[upload_id]["parts"] = {1: b"0123456789"}
    monkeypatch.setattr(dm_compact, "get_object_from_s3", _Fetches(fail_on="logs/x/05.log"))
    continuation = {
        "upload_id": upload_id,
        "key": "compacted/x/compacted-x.log",
        "start_after": "logs/x/00.log",
        "part_number": 2,
    }

    with pytest.raises(RuntimeError, match="logs/x/05.log"):
        dm_compact.merge_objects_from_s3(
            "src", "logs/x/", "dst", "compacted/x", f"{tmp_path}/", continuation=continuation
        )

    assert fake.aborted == []
    assert upload_id in fake.uploads
    assert list(tmp_path.iterdir()) == []


def test_lambda_handler_loops_the_map_while_a_continuation_is_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(dm_compact, "FETCH_CONCURRENCY", 1)
    objects = {f"logs/x/{i:02d}.log": f"{i:02d}-".encode() for i in range(8)}
    fake = _fake_multipart_s3(monkeypatch, dm_compact, objects, part_bytes=10, min_part_bytes=5)
    monkeypatch.setattr(dm_compact, "CHECKPOINT_MARGIN_SECONDS", 1)

    class Context:
        """A clock that loses a second per poll, so the first invocation runs out after a few objects."""

        def __init__(self, remaining_ms):
            self.remaining_ms = remaining_ms

        def get_remaining_time_in_millis(self):
            self.remaining_ms -= 1000
            return self.remaining_ms

    event = {"src": '"s3://src/logs/x/"', "dest": '"s3://dst/compacted/x"'}
    first = dm_compact.lambda_handler(event, Context(remaining_ms=6000))
    assert first["src"] == event["src"] and first["dest"] == event["dest"]
    assert first["continuation"]["part_number"] >= 2

    second = dm_compact.lambda_handler(first, Context(remaining_ms=10**9))
    assert second == {"statusCode": 200, "body": "Compaction complete!"}
    [(key, content, _)] = fake.completed
    assert key == "compacted/x/compacted-x.log"
    assert content == b"".join(objects[k] for k in sorted(objects))


def test_list_objects_is_lazy_and_resumes_after_a_key(monkeypatch):
    pages = [
        {"Contents": [{"Key": "a"}, {"Key": "b"}], "NextContinuationToken": "t1"},
        {"Contents": [{"Key": "c"}]},
    ]
    calls = []

    def list_objects_v2(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    monkeypatch.setattr(dm_compact.s3, "list_objects_v2", list_objects_v2)

    listing = dm_compact.list_objects_in_s3("bkt", "p/", start_after="0")
    assert next(listing) == {"Key": "a"}
    assert len(calls) == 1 and calls[0] == {"Bucket": "bkt", "Prefix": "p/", "StartAfter": "0"}
    assert [obj["Key"] for obj in listing] == ["b", "c"]
    assert calls[1]["ContinuationToken"] == "t1"
