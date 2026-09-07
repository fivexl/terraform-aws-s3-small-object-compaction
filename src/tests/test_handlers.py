"""Unit tests for the pure helper functions shared by the Lambda handlers.

The three handler packages each ship an ``index.py``, so they are loaded under
distinct module names via importlib instead of a plain import.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# The handlers create a boto3 client at import time, which needs a region but
# no credentials
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")

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
