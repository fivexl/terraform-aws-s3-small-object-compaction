variable "name_prefix" {
  description = "Prefix used for naming all resources created by this module"
  type        = string
  default     = "s3-object-compaction"
}

variable "source_s3_uri" {
  description = "S3 URI holding the small objects to compact, e.g. s3://my-bucket/raw/. Date prefixes are appended to it"
  type        = string

  validation {
    condition     = startswith(var.source_s3_uri, "s3://")
    error_message = "source_s3_uri must start with s3://"
  }
}

variable "target_s3_uri" {
  description = "S3 URI where compacted objects are written, e.g. s3://my-bucket/compacted/. Date prefixes are appended to it"
  type        = string

  validation {
    condition     = startswith(var.target_s3_uri, "s3://")
    error_message = "target_s3_uri must start with s3://"
  }
}

variable "previous_days" {
  description = "How many days back to compact. Each daily date prefix in the range is compacted into one object"
  type        = number
  default     = 1

  validation {
    condition     = var.previous_days >= 1
    error_message = "previous_days must be at least 1."
  }
}

variable "date_format" {
  description = "Python strftime format of the date prefixes under the source URI, e.g. %Y/%m/%d for year/month/day"
  type        = string
  default     = "%Y/%m/%d"
}

variable "create_standalone_lambda" {
  description = "Create the standalone compaction Lambda variant that processes all prefixes in a single invocation"
  type        = bool
  default     = true
}

variable "create_step_functions" {
  description = "Create the Step Functions variant that compacts prefixes in parallel with a Distributed Map"
  type        = bool
  default     = true
}

variable "schedule_enabled" {
  description = "Enable the EventBridge schedules. Disabled by default, matching the upstream solution"
  type        = bool
  default     = false
}

variable "schedule_expression" {
  description = "EventBridge schedule expression. Defaults to rate(previous_days days) when null"
  type        = string
  default     = null
}

variable "lambda_runtime" {
  description = "Python runtime used by all Lambda functions"
  type        = string
  default     = "python3.12"
}

variable "standalone_lambda_memory_size" {
  description = "Memory size in MB for the standalone compaction Lambda"
  type        = number
  default     = 1024
}

variable "standalone_lambda_timeout" {
  description = "Timeout in seconds for the standalone compaction Lambda"
  type        = number
  default     = 900
}

variable "list_lambda_memory_size" {
  description = "Memory size in MB for the prefix-listing Lambda used by Step Functions"
  type        = number
  default     = 128
}

variable "list_lambda_timeout" {
  description = "Timeout in seconds for the prefix-listing Lambda used by Step Functions"
  type        = number
  default     = 60
}

variable "compact_lambda_memory_size" {
  description = "Memory size in MB for the per-prefix compaction Lambda used by Step Functions"
  type        = number
  default     = 128
}

variable "compact_lambda_timeout" {
  description = "Timeout in seconds for the per-prefix compaction Lambda used by Step Functions"
  type        = number
  default     = 300
}

variable "lambda_ephemeral_storage_size" {
  description = "Ephemeral storage (/tmp) in MB for the compaction Lambdas. Must fit a full day of merged objects"
  type        = number
  default     = 2048
}

variable "sfn_max_concurrency" {
  description = "Maximum concurrent child executions of the Distributed Map"
  type        = number
  default     = 100
}

variable "cloudwatch_logs_retention_in_days" {
  description = "Retention in days for the Lambda and Step Functions CloudWatch log groups"
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags applied to all resources created by this module"
  type        = map(string)
  default     = {}
}
