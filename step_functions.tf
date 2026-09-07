locals {
  state_machine_name = "${var.name_prefix}-state-machine"
  # Constructed instead of referenced so the role policy can point at the state
  # machine without a circular dependency (the Distributed Map starts child
  # executions of its own state machine)
  state_machine_arn = "arn:${data.aws_partition.current.partition}:states:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.state_machine_name}"

  lambda_invoke_retry = [
    {
      ErrorEquals = [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
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
      ForEachS3Prefix = {
        Type = "Map"
        ItemProcessor = {
          ProcessorConfig = {
            Mode          = "DISTRIBUTED"
            ExecutionType = "EXPRESS"
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
      }
    }
  })
}

module "list_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

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
  version = "~> 8.0"

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

# Vended execution logs carry no payload secrets; a customer-managed key is
# left to the consumer's account-level CloudWatch encryption policy
#tfsec:ignore:aws-cloudwatch-log-group-customer-key
resource "aws_cloudwatch_log_group" "state_machine" {
  count = var.create_step_functions ? 1 : 0

  # The /aws/vendedlogs/ prefix keeps Step Functions logging inside the shared
  # CloudWatch Logs resource policy size limit
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.cloudwatch_logs_retention_in_days

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

  statement {
    sid       = "ManageDistributedMapChildExecutions"
    actions   = ["states:DescribeExecution", "states:StopExecution"]
    resources = ["${local.state_machine_arn}:*"]
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

resource "aws_sfn_state_machine" "compaction" {
  count = var.create_step_functions ? 1 : 0

  name       = local.state_machine_name
  role_arn   = aws_iam_role.state_machine[0].arn
  definition = local.state_machine_definition

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine[0].arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy.state_machine]
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
    resources = [aws_sfn_state_machine.compaction[0].arn]
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
  arn      = aws_sfn_state_machine.compaction[0].arn
  role_arn = aws_iam_role.events[0].arn
  input    = local.schedule_input
}
