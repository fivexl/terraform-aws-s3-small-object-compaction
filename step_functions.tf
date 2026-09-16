locals {
  state_machine_name = "${var.name_prefix}-state-machine"
  # Constructed instead of referenced so the role policy can point at the state
  # machine without a circular dependency (the Distributed Map starts child
  # executions of its own state machine)
  states_arn_prefix = "arn:${data.aws_partition.current.partition}:states:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
  state_machine_arn = "${local.states_arn_prefix}:stateMachine:${local.state_machine_name}"

  # Every ARN shape Step Functions can present for this one state machine as
  # aws:SourceArn when it assumes the role: the state machine itself (parent
  # execution), Standard executions, the Map Run, and Express executions (the
  # Distributed Map children). Allowing only a subset denies whichever caller
  # presents the missing shape, which is how v0.3.0 and v0.3.1 broke the Map
  state_machine_source_arns = [
    local.state_machine_arn,
    "${local.states_arn_prefix}:execution:${local.state_machine_name}:*",
    "${local.states_arn_prefix}:mapRun:${local.state_machine_name}/*",
    "${local.states_arn_prefix}:express:${local.state_machine_name}/*",
  ]

  lambda_invoke_retry = [
    {
      # Timeouts are retried too: compaction is idempotent, so a re-run of a
      # date that timed out overwrites its partial output instead of appending
      ErrorEquals = [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
        "Lambda.Unknown",
        "States.Timeout",
      ]
      IntervalSeconds = 1
      MaxAttempts     = 3
      BackoffRate     = 2
    },
  ]

  state_machine_definition = jsonencode({
    StartAt = "GetListofPrefixes"
    States = {
      GetListofPrefixes = {
        Type     = "Task"
        Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"
        Parameters = {
          "Payload.$"  = "$"
          FunctionName = "${module.list_lambda.lambda_function_arn}:$LATEST"
        }
        Retry      = local.lambda_invoke_retry
        Next       = "ForEachS3Prefix"
        OutputPath = "$.Payload"
      }
      # Tolerated-failure keys are only emitted when set: Step Functions
      # rejects a null value, and omitting them keeps its default of zero
      ForEachS3Prefix = merge({
        Type = "Map"
        ItemProcessor = {
          ProcessorConfig = {
            Mode          = "DISTRIBUTED"
            ExecutionType = var.sfn_child_execution_type
          }
          StartAt = "CompactFilesInPrefix"
          States = {
            CompactFilesInPrefix = {
              Type       = "Task"
              Resource   = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"
              OutputPath = "$.Payload"
              Parameters = {
                "Payload.$"  = "$"
                FunctionName = "${module.compact_lambda.lambda_function_arn}:$LATEST"
              }
              Retry = local.lambda_invoke_retry
              End   = true
            }
          }
        }
        MaxConcurrency = var.sfn_max_concurrency
        Label          = "ForEachS3Prefix"
        End            = true
        ItemReader = {
          Resource = "arn:${data.aws_partition.current.partition}:states:::s3:getObject"
          ReaderConfig = {
            InputType = "JSONL"
          }
          Parameters = {
            "Bucket.$" = "$.s3_locations_bucket"
            "Key.$"    = "$.s3_locations_key"
          }
        }
        },
        var.sfn_tolerated_failure_count == null ? {} : { ToleratedFailureCount = var.sfn_tolerated_failure_count },
        var.sfn_tolerated_failure_percentage == null ? {} : { ToleratedFailurePercentage = var.sfn_tolerated_failure_percentage },
      )
    }
  })
}

module "list_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create = var.create_step_functions

  function_name = "${var.name_prefix}-list-prefixes"
  description   = "Lists date prefixes to compact and writes the manifest consumed by the Distributed Map"
  handler       = "index.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.list_lambda_timeout
  memory_size   = var.list_lambda_memory_size

  source_path = "${path.module}/src/distributed_map_list"

  tracing_mode          = "Active"
  attach_tracing_policy = true

  attach_policy_statements = true
  policy_statements        = local.s3_policy_statements

  cloudwatch_logs_retention_in_days = var.cloudwatch_logs_retention_in_days

  tags = var.tags
}

module "compact_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create = var.create_step_functions

  function_name = "${var.name_prefix}-compact"
  description   = "Compacts the small S3 objects of a single prefix into one larger object"
  handler       = "index.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.compact_lambda_timeout
  memory_size   = var.compact_lambda_memory_size

  ephemeral_storage_size = var.lambda_ephemeral_storage_size

  source_path = "${path.module}/src/distributed_map_compact"

  tracing_mode          = "Active"
  attach_tracing_policy = true

  attach_policy_statements = true
  policy_statements        = local.s3_policy_statements

  cloudwatch_logs_retention_in_days = var.cloudwatch_logs_retention_in_days

  tags = var.tags
}

data "aws_iam_policy_document" "state_machine_assume" {
  count = var.create_step_functions ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }

    # Confused-deputy guard. The role carries "*"-scoped log-delivery grants
    # that cannot be narrowed, so only this account's compaction state machine
    # may assume it, not any state machine a principal with iam:PassRole creates
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = local.state_machine_source_arns
    }
  }
}

data "aws_iam_policy_document" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  statement {
    sid     = "InvokeCompactionLambdas"
    actions = ["lambda:InvokeFunction"]
    resources = [
      module.list_lambda.lambda_function_arn,
      module.compact_lambda.lambda_function_arn,
      "${module.list_lambda.lambda_function_arn}:*",
      "${module.compact_lambda.lambda_function_arn}:*",
    ]
  }

  statement {
    sid       = "ReadPrefixManifest"
    actions   = ["s3:GetObject"]
    resources = ["${local.target_bucket_arn}/*"]
  }

  statement {
    sid       = "StartDistributedMapChildExecutions"
    actions   = ["states:StartExecution"]
    resources = [local.state_machine_arn]
  }

  # Child executions of the Distributed Map: execution: ARNs for STANDARD
  # children, express: ARNs for EXPRESS children
  statement {
    sid     = "ManageDistributedMapChildExecutions"
    actions = ["states:DescribeExecution", "states:StopExecution"]
    resources = [
      "${local.states_arn_prefix}:execution:${local.state_machine_name}:*",
      "${local.states_arn_prefix}:express:${local.state_machine_name}/*",
    ]
  }

  # CloudWatch Logs delivery for Step Functions supports only "*" resources
  statement {
    sid = "VendedLogDelivery"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }

  statement {
    sid = "XRayTracing"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  name               = "${local.state_machine_name}-role"
  assume_role_policy = data.aws_iam_policy_document.state_machine_assume[0].json

  tags = var.tags
}

resource "aws_iam_role_policy" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  name   = "compaction"
  role   = aws_iam_role.state_machine[0].id
  policy = data.aws_iam_policy_document.state_machine[0].json
}

# The module owns the state machine and its execution log group. It defaults
# the log group name to /aws/vendedlogs/states/<name>, the prefix that keeps
# Step Functions logging inside the shared CloudWatch Logs resource policy size
# limit. Vended execution logs carry no payload secrets, so no customer-managed
# key is set; leave that to the consumer's account-level CloudWatch encryption
# policy.
#
# The IAM role stays in this file instead of being created by the module. The
# module builds its own trust policy and cannot express the aws:SourceAccount /
# aws:SourceArn conditions above that guard the "*"-scoped log-delivery grants.
# With use_existing_role the module creates no IAM at all, so the role, its
# policy and its trust policy are untouched by the refactor
module "step_function" {
  source  = "terraform-aws-modules/step-functions/aws"
  version = "5.1.1"

  create = var.create_step_functions

  name       = local.state_machine_name
  type       = "STANDARD"
  definition = local.state_machine_definition

  create_role       = false
  use_existing_role = true
  role_arn          = var.create_step_functions ? aws_iam_role.state_machine[0].arn : ""

  cloudwatch_log_group_retention_in_days = var.cloudwatch_logs_retention_in_days

  logging_configuration = {
    include_execution_data = true
    level                  = "ALL"
  }

  # Passing xray is what switches on the module's tracing_configuration block.
  # With use_existing_role it attaches no policy; the role policy above already
  # carries the X-Ray statement
  service_integrations = {
    xray = {
      xray = true
    }
  }

  tags = var.tags

  # Step Functions checks the role's log-delivery permissions when the state
  # machine is created, so the role policy has to exist first
  depends_on = [aws_iam_role_policy.state_machine]
}

# Both resources carry over into the module unchanged. The IAM role and its
# policy are not moved: the module does not manage them
moved {
  from = aws_sfn_state_machine.compaction[0]
  to   = module.step_function.aws_sfn_state_machine.this[0]
}

moved {
  from = aws_cloudwatch_log_group.state_machine[0]
  to   = module.step_function.aws_cloudwatch_log_group.sfn[0]
}

data "aws_iam_policy_document" "events_assume" {
  count = var.create_step_functions ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "events_start_execution" {
  count = var.create_step_functions ? 1 : 0

  statement {
    actions   = ["states:StartExecution"]
    resources = [module.step_function.state_machine_arn]
  }
}

resource "aws_iam_role" "events" {
  count = var.create_step_functions ? 1 : 0

  name               = "${local.state_machine_name}-events-role"
  assume_role_policy = data.aws_iam_policy_document.events_assume[0].json

  tags = var.tags
}

resource "aws_iam_role_policy" "events" {
  count = var.create_step_functions ? 1 : 0

  name   = "start-execution"
  role   = aws_iam_role.events[0].id
  policy = data.aws_iam_policy_document.events_start_execution[0].json
}

resource "aws_cloudwatch_event_rule" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  name                = local.state_machine_name
  description         = "Triggers the S3 compaction Step Functions state machine"
  schedule_expression = local.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  rule     = aws_cloudwatch_event_rule.state_machine[0].name
  arn      = module.step_function.state_machine_arn
  role_arn = aws_iam_role.events[0].arn
  input    = local.schedule_input
}
