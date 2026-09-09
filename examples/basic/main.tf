# Throwaway demo buckets: SSE-S3 is enough, and logging/versioning would only
# get in the way of force_destroy
#tfsec:ignore:aws-s3-enable-bucket-logging #tfsec:ignore:aws-s3-enable-versioning
resource "aws_s3_bucket" "source" {
  bucket_prefix = "compaction-source-"
  force_destroy = true
}

#tfsec:ignore:aws-s3-enable-bucket-logging #tfsec:ignore:aws-s3-enable-versioning
resource "aws_s3_bucket" "target" {
  bucket_prefix = "compaction-target-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket = aws_s3_bucket.source.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "target" {
  bucket = aws_s3_bucket.target.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

#tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  bucket = aws_s3_bucket.source.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

#tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "target" {
  bucket = aws_s3_bucket.target.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

module "s3_small_object_compaction" {
  source  = "fivexl/s3-small-object-compaction/aws"
  version = ">= 0.2.0"

  source_s3_uri = "s3://${aws_s3_bucket.source.id}/data/"
  target_s3_uri = "s3://${aws_s3_bucket.target.id}/compacted/"

  previous_days = 7
  date_format   = "%Y/%m/%d"

  # Schedules are created disabled; flip this on once you have verified a
  # manual run against real data
  schedule_enabled = false

  tags = {
    Terraform = "true"
    Example   = "basic"
  }
}

output "state_machine_arn" {
  description = "ARN of the compaction state machine"
  value       = module.s3_small_object_compaction.state_machine_arn
}

output "standalone_lambda_function_name" {
  description = "Name of the standalone compaction Lambda"
  value       = module.s3_small_object_compaction.standalone_lambda_function_name
}
