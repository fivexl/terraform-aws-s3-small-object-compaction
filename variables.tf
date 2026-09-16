variable "name_prefix" {
  description = "Prefix used for naming all resources created by this module"
  type        = string
  default     = "s3-object-compaction"
}

variable "source_s3_uri" {
  description = "S3 URI holding the small objects to compact, e.g. s3://my-bucket/raw/. Date prefixes are appended to it. A key prefix is required: the Lambda IAM policies are scoped to it"
  type        = string

  validation {
    condition     = can(regex("^s3://[^/]+/.+", var.source_s3_uri))
    error_message = "source_s3_uri must be of the form s3://<bucket>/<prefix>. A bare bucket URI is rejected because the Lambda read permissions are scoped to the prefix."
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
  description = "Ephemeral storage (/tmp) in MB for the compaction Lambdas, 512 to 10240. The merged bytes of a single date prefix are staged in /tmp before upload, so this must exceed the largest day of source data under any one prefix, or that date fails mid-write with 'No space left on device'"
  type        = number
  default     = 2048
}

variable "sfn_max_concurrency" {
  description = "Maximum concurrent child executions of the Distributed Map"
  type        = number
  default     = 100
}

variable "sfn_child_execution_type" {
  description = "Execution type of the Distributed Map child workflows. EXPRESS caps each date prefix at 5 minutes total regardless of compact_lambda_timeout, which a busy date on a large source cannot meet. STANDARD lifts that ceiling at Standard-workflow pricing"
  type        = string
  default     = "EXPRESS"

  validation {
    condition     = contains(["EXPRESS", "STANDARD"], var.sfn_child_execution_type)
    error_message = "sfn_child_execution_type must be EXPRESS or STANDARD."
  }
}

variable "sfn_tolerated_failure_count" {
  description = "Number of failed date prefixes the Distributed Map tolerates before the whole execution fails. Unset keeps the Step Functions default of zero, where one failed date fails the run after other dates have already written their output"
  type        = number
  default     = null
}

variable "sfn_tolerated_failure_percentage" {
  description = "Percentage (0-100) of failed date prefixes the Distributed Map tolerates before the whole execution fails. Unset keeps the Step Functions default of zero. If both count and percentage are set the run fails when either is exceeded"
  type        = number
  default     = null

  validation {
    condition     = var.sfn_tolerated_failure_percentage == null || (var.sfn_tolerated_failure_percentage >= 0 && var.sfn_tolerated_failure_percentage <= 100)
    error_message = "sfn_tolerated_failure_percentage must be between 0 and 100."
  }
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
