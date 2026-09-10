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

# The module owns the state machine, its IAM role and its execution log group.
# It defaults the log group name to /aws/vendedlogs/states/<name>, which is the
# prefix that keeps Step Functions logging inside the shared CloudWatch Logs
# resource policy size limit, and its attach_cloudwatch_logs_policy default
# emits the vended-log-delivery statement. Vended execution logs carry no
# payload secrets, so no customer-managed key is set here; leave that to the
# consumer's account-level CloudWatch encryption policy
module "step_function" {
  source  = "terraform-aws-modules/step-functions/aws"
  version = "5.1.1"

  create = var.create_step_functions

  name       = local.state_machine_name
  type       = "STANDARD"
  definition = local.state_machine_definition

  role_name = "${local.state_machine_name}-role"

  cloudwatch_log_group_retention_in_days = var.cloudwatch_logs_retention_in_days

  logging_configuration = {
    include_execution_data = true
    level                  = "ALL"
  }

  # Resources are passed as explicit lists rather than `true`: the lambda and
  # stepfunction integrations ship no default_resources. The state machine ARN
  # stays the constructed local so the role policy does not depend on the state
  # machine whose child executions the Distributed Map starts
  service_integrations = {
    lambda = {
      lambda = [
        module.list_lambda.lambda_function_arn,
        module.compact_lambda.lambda_function_arn,
        "${module.list_lambda.lambda_function_arn}:*",
        "${module.compact_lambda.lambda_function_arn}:*",
      ]
    }

    stepfunction = {
      stepfunction = [local.state_machine_arn]
    }

    # Passing xray is also what switches on tracing_configuration in the module
    xray = {
      xray = true
    }
  }

  # No service integration covers these two. stepfunction_Sync is not a
  # substitute for the second one: it would also grant states:StartSyncExecution,
  # which this state machine never calls
  attach_policy_statements = true
  policy_statements = {
    read_prefix_manifest = {
      sid       = "ReadPrefixManifest"
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${local.target_bucket_arn}/*"]
    }
    manage_distributed_map_child_executions = {
      sid       = "ManageDistributedMapChildExecutions"
      effect    = "Allow"
      actions   = ["states:DescribeExecution", "states:StopExecution"]
      resources = ["${local.state_machine_arn}:*"]
    }
  }

  tags = var.tags
}

# The state machine, its role and its log group carry over into the module
# untouched. aws_iam_role_policy has no counterpart to move to -- the module
# models the same permissions as managed policies, a different resource type --
# so that one inline policy is destroyed and replaced on apply. The set of
# granted actions and resources is unchanged
moved {
  from = aws_sfn_state_machine.compaction[0]
  to   = module.step_function.aws_sfn_state_machine.this[0]
}

moved {
  from = aws_iam_role.state_machine[0]
  to   = module.step_function.aws_iam_role.this[0]
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
