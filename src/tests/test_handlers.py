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
