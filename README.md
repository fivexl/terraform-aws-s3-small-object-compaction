[![FivexL](https://releases.fivexl.io/like-this-repo-banner.png)](https://fivexl.io/#email-subscription)

### Want practical AWS infrastructure insights?

👉 [Subscribe to our newsletter](https://fivexl.io/#email-subscription) to get:

- Real stories from real AWS projects
- No-nonsense DevOps tactics
- Cost, security & compliance patterns that actually work
- Expert guidance from engineers in the field

=========================================================================

# Amazon S3 Small Object Compaction

Terraform module to compact small S3 objects into larger files with AWS Lambda and AWS Step Functions, cutting storage costs on tiers with minimum billable object sizes (e.g. 128 KB) and speeding up Amazon Athena queries.

## Acknowledgments

This project is based on [aws-samples/s3-small-object-compaction](https://github.com/aws-samples/s3-small-object-compaction). We thank the original contributors for their work on the CDK-based solution that inspired this Terraform module.

## Architecture

The module deploys two variants of the compaction solution:

1. A **standalone Lambda function** (via [terraform-aws-modules/lambda/aws](https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws)) that iterates over a list of Amazon S3 prefixes and compacts the objects in each into a single larger file
2. An **AWS Step Functions state machine** (via [terraform-aws-modules/step-functions/aws](https://registry.terraform.io/modules/terraform-aws-modules/step-functions/aws)) using [Distributed Map](https://docs.aws.amazon.com/step-functions/latest/dg/use-dist-map-orchestrate-large-scale-parallel-workloads.html) to invoke a compaction Lambda in parallel for each prefix, for faster compaction at scale

Both variants can be triggered on an **EventBridge schedule** (disabled by default, matching the upstream solution).

### Permissions

The handlers read the source and destination URIs from the invocation event, so the scheduled payload is not a permission boundary. The Lambda execution roles are therefore granted `s3:ListBucket` and `s3:GetObject` only on the key prefix of `source_s3_uri`, and an invocation that points at another prefix fails with `AccessDenied`. For that reason `source_s3_uri` must include a key prefix; a bare `s3://bucket/` is rejected at plan time. The functions never delete source objects.

## Usage

See [examples/basic](./examples/basic).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.5.7 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.28 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.63.0 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_compact_lambda"></a> [compact\_lambda](#module\_compact\_lambda) | terraform-aws-modules/lambda/aws | 8.8.0 |
| <a name="module_list_lambda"></a> [list\_lambda](#module\_list\_lambda) | terraform-aws-modules/lambda/aws | 8.8.0 |
| <a name="module_standalone_compact_lambda"></a> [standalone\_compact\_lambda](#module\_standalone\_compact\_lambda) | terraform-aws-modules/lambda/aws | 8.8.0 |
| <a name="module_step_function"></a> [step\_function](#module\_step\_function) | terraform-aws-modules/step-functions/aws | 5.1.1 |

## Resources

| Name | Type |
|------|------|
| [aws_cloudwatch_event_rule.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_iam_role.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_lambda_permission.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.events_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.events_start_execution](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_partition.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/partition) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_cloudwatch_logs_retention_in_days"></a> [cloudwatch\_logs\_retention\_in\_days](#input\_cloudwatch\_logs\_retention\_in\_days) | Retention in days for the Lambda and Step Functions CloudWatch log groups | `number` | `14` | no |
| <a name="input_compact_lambda_memory_size"></a> [compact\_lambda\_memory\_size](#input\_compact\_lambda\_memory\_size) | Memory size in MB for the per-prefix compaction Lambda used by Step Functions | `number` | `128` | no |
| <a name="input_compact_lambda_timeout"></a> [compact\_lambda\_timeout](#input\_compact\_lambda\_timeout) | Timeout in seconds for the per-prefix compaction Lambda used by Step Functions | `number` | `300` | no |
| <a name="input_create_standalone_lambda"></a> [create\_standalone\_lambda](#input\_create\_standalone\_lambda) | Create the standalone compaction Lambda variant that processes all prefixes in a single invocation | `bool` | `true` | no |
| <a name="input_create_step_functions"></a> [create\_step\_functions](#input\_create\_step\_functions) | Create the Step Functions variant that compacts prefixes in parallel with a Distributed Map | `bool` | `true` | no |
| <a name="input_date_format"></a> [date\_format](#input\_date\_format) | Python strftime format of the date prefixes under the source URI, e.g. %Y/%m/%d for year/month/day | `string` | `"%Y/%m/%d"` | no |
| <a name="input_lambda_ephemeral_storage_size"></a> [lambda\_ephemeral\_storage\_size](#input\_lambda\_ephemeral\_storage\_size) | Ephemeral storage (/tmp) in MB for the compaction Lambdas. Must fit a full day of merged objects | `number` | `2048` | no |
| <a name="input_lambda_runtime"></a> [lambda\_runtime](#input\_lambda\_runtime) | Python runtime used by all Lambda functions | `string` | `"python3.12"` | no |
| <a name="input_list_lambda_memory_size"></a> [list\_lambda\_memory\_size](#input\_list\_lambda\_memory\_size) | Memory size in MB for the prefix-listing Lambda used by Step Functions | `number` | `128` | no |
| <a name="input_list_lambda_timeout"></a> [list\_lambda\_timeout](#input\_list\_lambda\_timeout) | Timeout in seconds for the prefix-listing Lambda used by Step Functions | `number` | `60` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix used for naming all resources created by this module | `string` | `"s3-object-compaction"` | no |
| <a name="input_previous_days"></a> [previous\_days](#input\_previous\_days) | How many days back to compact. Each daily date prefix in the range is compacted into one object | `number` | `1` | no |
| <a name="input_schedule_enabled"></a> [schedule\_enabled](#input\_schedule\_enabled) | Enable the EventBridge schedules. Disabled by default, matching the upstream solution | `bool` | `false` | no |
| <a name="input_schedule_expression"></a> [schedule\_expression](#input\_schedule\_expression) | EventBridge schedule expression. Defaults to rate(previous\_days days) when null | `string` | `null` | no |
| <a name="input_sfn_max_concurrency"></a> [sfn\_max\_concurrency](#input\_sfn\_max\_concurrency) | Maximum concurrent child executions of the Distributed Map | `number` | `100` | no |
| <a name="input_source_s3_uri"></a> [source\_s3\_uri](#input\_source\_s3\_uri) | S3 URI holding the small objects to compact, e.g. s3://my-bucket/raw/. Date prefixes are appended to it. A key prefix is required: the Lambda IAM policies are scoped to it | `string` | n/a | yes |
| <a name="input_standalone_lambda_memory_size"></a> [standalone\_lambda\_memory\_size](#input\_standalone\_lambda\_memory\_size) | Memory size in MB for the standalone compaction Lambda | `number` | `1024` | no |
| <a name="input_standalone_lambda_timeout"></a> [standalone\_lambda\_timeout](#input\_standalone\_lambda\_timeout) | Timeout in seconds for the standalone compaction Lambda | `number` | `900` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to all resources created by this module | `map(string)` | `{}` | no |
| <a name="input_target_s3_uri"></a> [target\_s3\_uri](#input\_target\_s3\_uri) | S3 URI where compacted objects are written, e.g. s3://my-bucket/compacted/. Date prefixes are appended to it | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_compact_lambda_function_arn"></a> [compact\_lambda\_function\_arn](#output\_compact\_lambda\_function\_arn) | ARN of the per-prefix compaction Lambda, empty string when create\_step\_functions is false |
| <a name="output_list_lambda_function_arn"></a> [list\_lambda\_function\_arn](#output\_list\_lambda\_function\_arn) | ARN of the prefix-listing Lambda, empty string when create\_step\_functions is false |
| <a name="output_schedule_expression"></a> [schedule\_expression](#output\_schedule\_expression) | EventBridge schedule expression used by both trigger rules |
| <a name="output_standalone_lambda_function_arn"></a> [standalone\_lambda\_function\_arn](#output\_standalone\_lambda\_function\_arn) | ARN of the standalone compaction Lambda, empty string when create\_standalone\_lambda is false |
| <a name="output_standalone_lambda_function_name"></a> [standalone\_lambda\_function\_name](#output\_standalone\_lambda\_function\_name) | Name of the standalone compaction Lambda, empty string when create\_standalone\_lambda is false |
| <a name="output_state_machine_arn"></a> [state\_machine\_arn](#output\_state\_machine\_arn) | ARN of the compaction Step Functions state machine, null when create\_step\_functions is false |
| <a name="output_state_machine_name"></a> [state\_machine\_name](#output\_state\_machine\_name) | Name of the compaction Step Functions state machine, null when create\_step\_functions is false |
| <a name="output_state_machine_role_arn"></a> [state\_machine\_role\_arn](#output\_state\_machine\_role\_arn) | ARN of the IAM role assumed by the state machine, null when create\_step\_functions is false |
<!-- END_TF_DOCS -->

## License

Apache 2.0 Licensed. See [LICENSE](./LICENSE) for full details.
