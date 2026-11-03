data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

data "aws_region" "current" {
  # Plan-time check of the parquet mode, which has no resource of its own to
  # carry it. A precondition rather than a check block, which the tfsec in CI
  # cannot parse
  lifecycle {
    # pyarrow needs far more than the 128 MB the per-prefix function defaults
    # to: at 128 MB a merge of one day of flow logs was OOM-killed
    precondition {
      condition = !local.parquet || (
        (!var.create_standalone_lambda || var.standalone_lambda_memory_size >= 512) &&
        (!var.create_step_functions || var.compact_lambda_memory_size >= 512)
      )
      error_message = "file_format = \"parquet\" loads pyarrow in the compaction functions: set standalone_lambda_memory_size and compact_lambda_memory_size to at least 512, or the functions run out of memory."
    }
  }
}

locals {
  source_bucket = split("/", trimprefix(var.source_s3_uri, "s3://"))[0]
  target_bucket = split("/", trimprefix(var.target_s3_uri, "s3://"))[0]

  # Key prefix under the source bucket. Validation on source_s3_uri guarantees
  # it is non-empty, so the IAM grants below never widen to the whole bucket
  source_prefix = trimprefix(trimprefix(var.source_s3_uri, "s3://"), "${local.source_bucket}/")

  source_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${local.source_bucket}"
  target_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${local.target_bucket}"

  # rate() expressions require the singular unit for a value of 1
  schedule_expression = coalesce(
    var.schedule_expression,
    var.previous_days == 1 ? "rate(1 day)" : "rate(${var.previous_days} days)"
  )

  # Payload shape expected by both the standalone handler and the state machine
  schedule_input = jsonencode({
    s3_source_uri      = var.source_s3_uri
    s3_destination_uri = var.target_s3_uri
    date_format        = var.date_format
    duration           = var.previous_days
  })

  # Explicit encryption header on every write when configured; a bucket default
  # encrypts the object but does not satisfy a policy that requires the header
  lambda_environment = var.target_sse_algorithm == null ? {} : { TARGET_SSE_ALGORITHM = var.target_sse_algorithm }

  # The compaction functions also learn how many objects to fetch at once and
  # the merge mode, and get pyarrow from the layer below in parquet mode. The
  # mode stays unset in concat mode so existing deployments see no
  # configuration change for it
  parquet = var.file_format == "parquet"

  compact_lambda_environment = merge(
    local.lambda_environment,
    { FETCH_CONCURRENCY = tostring(var.fetch_concurrency) },
    local.parquet ? { FILE_FORMAT = "parquet" } : {},
  )
  compact_lambda_layers = local.parquet ? [module.pyarrow_layer.lambda_layer_arn] : null

  # Statements shared by every function: read the source prefix, write the
  # compacted output (the list function also writes its prefix manifest there).
  # The handlers take s3_source_uri from the invocation event, so IAM, not the
  # scheduled payload, is what bounds the functions to the configured prefix
  s3_policy_statements = {
    source_list = {
      effect    = "Allow"
      actions   = ["s3:ListBucket"]
      resources = [local.source_bucket_arn]
      condition = [{
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${local.source_prefix}*"]
      }]
    }
    source_read = {
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${local.source_bucket_arn}/${local.source_prefix}*"]
    }
    target_write = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = [local.target_bucket_arn, "${local.target_bucket_arn}/*"]
    }
  }
}

# pyarrow for the parquet mode, built from the PyPI wheel instead of taken from
# the AWS SDK for pandas managed layer. That layer also ships pandas, numpy and
# their dependencies, none of which the handlers import, and its patches wait
# for an aws-sdk-pandas release: 3.17.1 bundles a certifi that still trusts the
# root of compromised eMudhra intermediates (GHSA-6q48-6c7w-9gw2). pyarrow has
# no dependencies, so this layer carries nothing else.
#
# pip runs on the machine that applies, so it needs python3, pip and access to
# PyPI. It fetches the manylinux wheel for the function runtime rather than the
# host's own, and compiles no bytecode for the host's Python; the functions use
# the Lambda default architecture, x86_64. The unpacked wheel stays in a
# per-name_prefix directory under $TMPDIR, cleared on the next build, because
# the zip step reads it after the shell exits.
#
# The patterns drop what the merge never loads, taking the zip from the 49 MB
# of the wheel to 35 MB: headers, Cython sources and tests, Arrow Flight, and
# the two extra copies of each libarrow_python library (a zip cannot hold
# symlinks). Every library left is a load-time dependency of pyarrow itself
module "pyarrow_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.12.0"

  create       = local.parquet
  create_layer = true

  layer_name               = "${var.name_prefix}-pyarrow"
  description              = "pyarrow for the Parquet merge of the S3 compaction functions"
  runtime                  = var.lambda_runtime
  compatible_architectures = ["x86_64"]

  source_path = [{
    path = "${path.module}/src/pyarrow_layer"
    commands = [
      "set -e",
      "build_dir=\"$${TMPDIR:-/tmp}/${var.name_prefix}-pyarrow-layer\"",
      "rm -rf \"$build_dir\"",
      "python3 -m pip install --quiet --disable-pip-version-check --no-compile --only-binary=:all: --platform manylinux_2_28_x86_64 --implementation cp --python-version ${trimprefix(var.lambda_runtime, "python")} --target \"$build_dir/python\" --requirement requirements.txt",
      "cd \"$build_dir\"",
      ":zip",
    ]
    patterns = [
      "!python/pyarrow/(include|includes|src|tests)/.*",
      "!python/pyarrow/.*\\.(pxd|pxi|pyx)",
      "!python/pyarrow/_pyarrow_cpp_tests\\..*",
      "!python/pyarrow/_flight\\..*",
      "!python/pyarrow/lib(arrow_flight|arrow_python_flight)\\.so.*",
      "!python/pyarrow/libarrow_python(_parquet_encryption)?\\.so(\\.[0-9]+\\.[0-9]+\\.[0-9]+)?",
    ]
  }]

  tags = var.tags
}
