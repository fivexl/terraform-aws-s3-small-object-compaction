data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

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

  # The compaction functions also learn the merge mode, and get pyarrow from the
  # AWS SDK for pandas layer in parquet mode. Both stay unset in concat mode so
  # existing deployments see no configuration change
  parquet = var.file_format == "parquet"

  compact_lambda_environment = merge(local.lambda_environment, local.parquet ? { FILE_FORMAT = "parquet" } : {})
  compact_lambda_layers      = local.parquet ? [data.aws_ssm_parameter.aws_sdk_pandas_layer[0].insecure_value] : null

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

# Published by AWS for every commercial Region, Python version and architecture.
# The functions use the Lambda default architecture, x86_64
data "aws_ssm_parameter" "aws_sdk_pandas_layer" {
  count = local.parquet ? 1 : 0

  name = "/aws/service/aws-sdk-pandas/${var.aws_sdk_pandas_version}/${replace(var.lambda_runtime, "python", "py")}/x86_64/layer-arn"

  # pyarrow and the layer need far more than the 128 MB the per-prefix function
  # defaults to: at 128 MB a merge of one day of flow logs was OOM-killed, and
  # the layer documentation names 512 MB as the floor. A precondition rather than
  # a check block, which the tfsec in CI cannot parse
  lifecycle {
    precondition {
      condition = (
        (!var.create_standalone_lambda || var.standalone_lambda_memory_size >= 512) &&
        (!var.create_step_functions || var.compact_lambda_memory_size >= 512)
      )
      error_message = "file_format = \"parquet\" loads pyarrow in the compaction functions: set standalone_lambda_memory_size and compact_lambda_memory_size to at least 512, or the functions run out of memory."
    }
  }
}
