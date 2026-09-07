data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  source_bucket = split("/", trimprefix(var.source_s3_uri, "s3://"))[0]
  target_bucket = split("/", trimprefix(var.target_s3_uri, "s3://"))[0]

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

  # Statements shared by every function: read the source prefix, write the
  # compacted output (the list function also writes its prefix manifest there)
  s3_policy_statements = {
    source_read = {
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:ListBucket"]
      resources = [local.source_bucket_arn, "${local.source_bucket_arn}/*"]
    }
    target_write = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = [local.target_bucket_arn, "${local.target_bucket_arn}/*"]
    }
  }
}
