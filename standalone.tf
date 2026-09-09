module "standalone_compact_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create = var.create_standalone_lambda

  function_name = "${var.name_prefix}-standalone-compact"
  description   = "Compacts small S3 objects from every date prefix in range into single larger objects"
  handler       = "index.handler"
  runtime       = var.lambda_runtime
  timeout       = var.standalone_lambda_timeout
  memory_size   = var.standalone_lambda_memory_size

  ephemeral_storage_size = var.lambda_ephemeral_storage_size

  source_path = "${path.module}/src/standalone_function_compact"

  tracing_mode          = "Active"
  attach_tracing_policy = true

  attach_policy_statements = true
  policy_statements        = local.s3_policy_statements

  cloudwatch_logs_retention_in_days = var.cloudwatch_logs_retention_in_days

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "standalone" {
  count = var.create_standalone_lambda ? 1 : 0

  name                = "${var.name_prefix}-standalone-compact"
  description         = "Triggers the standalone S3 compaction Lambda"
  schedule_expression = local.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "standalone" {
  count = var.create_standalone_lambda ? 1 : 0

  rule  = aws_cloudwatch_event_rule.standalone[0].name
  arn   = module.standalone_compact_lambda.lambda_function_arn
  input = local.schedule_input
}

resource "aws_lambda_permission" "standalone" {
  count = var.create_standalone_lambda ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.standalone_compact_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.standalone[0].arn
}
