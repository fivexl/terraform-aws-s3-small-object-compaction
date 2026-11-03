output "standalone_lambda_function_arn" {
  description = "ARN of the standalone compaction Lambda, empty string when create_standalone_lambda is false"
  value       = module.standalone_compact_lambda.lambda_function_arn
}

output "standalone_lambda_function_name" {
  description = "Name of the standalone compaction Lambda, empty string when create_standalone_lambda is false"
  value       = module.standalone_compact_lambda.lambda_function_name
}

output "list_lambda_function_arn" {
  description = "ARN of the prefix-listing Lambda, empty string when create_step_functions is false"
  value       = module.list_lambda.lambda_function_arn
}

output "compact_lambda_function_arn" {
  description = "ARN of the per-prefix compaction Lambda, empty string when create_step_functions is false"
  value       = module.compact_lambda.lambda_function_arn
}

output "state_machine_arn" {
  description = "ARN of the compaction Step Functions state machine, null when create_step_functions is false"
  value       = var.create_step_functions ? module.step_function.state_machine_arn : null
}

output "state_machine_name" {
  description = "Name of the compaction Step Functions state machine, null when create_step_functions is false"
  value       = var.create_step_functions ? module.step_function.state_machine_name : null
}

output "state_machine_role_arn" {
  description = "ARN of the IAM role assumed by the state machine, null when create_step_functions is false"
  value       = try(aws_iam_role.state_machine[0].arn, null)
}

output "schedule_expression" {
  description = "EventBridge schedule expression used by both trigger rules"
  value       = local.schedule_expression
}
