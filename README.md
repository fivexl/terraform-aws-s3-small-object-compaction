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

Within a prefix the compaction functions fetch `fetch_concurrency` objects at a time (default 16) and write them in listing order, so the compacted object is the same as a sequential merge. A date prefix of 80,000 small objects then fits in the 900 s Lambda maximum, where fetching one object at a time, at the ~65 ms a GET takes, stops at about 13,000 whatever the memory or timeout. Bodies waiting to be written are capped at 32 MB of memory, so a prefix of large objects is fetched a few at a time; set it to 1 for one fetch at a time.

### Permissions

The handlers read the source and destination URIs from the invocation event, so the scheduled payload is not a permission boundary. The Lambda execution roles are therefore granted `s3:ListBucket` and `s3:GetObject` only on the key prefix of `source_s3_uri`, and an invocation that points at another prefix fails with `AccessDenied`. For that reason `source_s3_uri` must include a key prefix; a bare `s3://bucket/` is rejected at plan time. The functions never delete source objects.

The state-machine role trusts `states.amazonaws.com` with an `aws:SourceAccount` check written as `StringEqualsIfExists`. Step Functions omits the confused-deputy context keys when it requests task credentials for Distributed Map child executions, so a strict condition denies every child; `IfExists` enforces the account check whenever the key is present and passes when the service omits it. A cross-account principal cannot hand the role to a state machine of their own, while a same-account principal already holding `iam:PassRole` on the role can, which is the posture of the upstream solution and of CDK-generated Distributed Map roles.

## File formats

By default (`file_format = "concat"`) the objects of a date prefix are merged by appending their bytes, which yields a valid file only for formats that survive concatenation: plain text, JSON Lines, header-less CSV and gzip. Parquet, ORC and Avro objects are detected by their magic bytes and the prefix fails with an error, rather than being written out as a file no reader can open.

`file_format = "parquet"` rewrites the row groups of every source object into a single Parquet file per prefix, keeping the source schema and compression codec and coalescing small row groups into ones of about 64 MB of in-memory data. A prefix whose objects have differing schemas, or which holds a non-Parquet object, fails instead of being merged. pyarrow comes from a Lambda layer the module builds at apply time from the PyPI wheel pinned in [`src/pyarrow_layer/requirements.txt`](./src/pyarrow_layer/requirements.txt), so the machine that runs `terraform apply` needs `python3`, `pip` and access to PyPI; the default `concat` mode builds nothing. The layer holds pyarrow alone, without the headers, tests and Arrow Flight libraries the merge never loads, in a 35 MB zip that stays under the 50 MB Lambda direct-upload limit. The parquet mode needs at least 512 MB for the compaction functions (`compact_lambda_memory_size` defaults to 128); the plan fails otherwise. This fits Parquet VPC Flow Logs:

```hcl
module "flow_logs_compaction" {
  source = "fivexl/s3-small-object-compaction/aws"

  source_s3_uri = "s3://my-log-archive/AWSLogs/111111111111/vpcflowlogs/us-east-2/"
  target_s3_uri = "s3://my-log-archive/compacted/vpcflowlogs/111111111111/"

  file_format                = "parquet"
  compact_lambda_memory_size = 512
}
```

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
| <a name="module_compact_lambda"></a> [compact\_lambda](#module\_compact\_lambda) | terraform-aws-modules/lambda/aws | 8.12.0 |
| <a name="module_list_lambda"></a> [list\_lambda](#module\_list\_lambda) | terraform-aws-modules/lambda/aws | 8.12.0 |
| <a name="module_pyarrow_layer"></a> [pyarrow\_layer](#module\_pyarrow\_layer) | terraform-aws-modules/lambda/aws | 8.12.0 |
| <a name="module_standalone_compact_lambda"></a> [standalone\_compact\_lambda](#module\_standalone\_compact\_lambda) | terraform-aws-modules/lambda/aws | 8.12.0 |
| <a name="module_step_function"></a> [step\_function](#module\_step\_function) | terraform-aws-modules/step-functions/aws | 5.1.1 |

## Resources

| Name | Type |
|------|------|
| [aws_cloudwatch_event_rule.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_iam_role.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_lambda_permission.standalone](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.events_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.events_start_execution](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.state_machine](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.state_machine_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
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
| <a name="input_file_format"></a> [file\_format](#input\_file\_format) | How the objects of a prefix are merged. concat appends their bytes, which is valid only for formats that survive concatenation: plain text, JSON Lines, header-less CSV and gzip. Parquet, ORC and Avro sources are refused in this mode instead of being written out corrupt. parquet rewrites the row groups of every source object into one Parquet file, with pyarrow from a Lambda layer the module builds from PyPI at apply time, so the applying machine needs python3, pip and access to PyPI. parquet needs at least 512 MB of memory for the compaction functions | `string` | `"concat"` | no |
| <a name="input_fetch_concurrency"></a> [fetch\_concurrency](#input\_fetch\_concurrency) | How many source objects the compaction functions fetch at once, 1 to 64. Objects are written in listing order whatever the value, so the compacted object is the same as a sequential merge. 1 fetches one object at a time, which at the ~65 ms one GET takes caps a date prefix at about 13,000 objects inside the 900 s Lambda maximum; the default lifts that past 100,000. Fetched bodies waiting to be written are capped at 32 MB of memory, so a prefix of large objects is fetched a few at a time | `number` | `16` | no |
| <a name="input_lambda_ephemeral_storage_size"></a> [lambda\_ephemeral\_storage\_size](#input\_lambda\_ephemeral\_storage\_size) | Ephemeral storage (/tmp) in MB for the compaction Lambdas, 512 to 10240. The merged bytes of a single date prefix are staged in /tmp before upload, so this must exceed the largest day of source data under any one prefix, or that date fails mid-write with 'No space left on device' | `number` | `2048` | no |
| <a name="input_lambda_runtime"></a> [lambda\_runtime](#input\_lambda\_runtime) | Python runtime used by all Lambda functions: python3.12, python3.13 or python3.14. The handlers need 3.12 for NamedTemporaryFile(delete\_on\_close), and the pyarrow pinned for the parquet layer publishes wheels up to 3.14 | `string` | `"python3.14"` | no |
| <a name="input_list_lambda_memory_size"></a> [list\_lambda\_memory\_size](#input\_list\_lambda\_memory\_size) | Memory size in MB for the prefix-listing Lambda used by Step Functions | `number` | `128` | no |
| <a name="input_list_lambda_timeout"></a> [list\_lambda\_timeout](#input\_list\_lambda\_timeout) | Timeout in seconds for the prefix-listing Lambda used by Step Functions | `number` | `60` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix used for naming all resources created by this module | `string` | `"s3-object-compaction"` | no |
| <a name="input_previous_days"></a> [previous\_days](#input\_previous\_days) | How many days back to compact. Each daily date prefix in the range is compacted into one object | `number` | `1` | no |
| <a name="input_schedule_enabled"></a> [schedule\_enabled](#input\_schedule\_enabled) | Enable the EventBridge schedules. Disabled by default, matching the upstream solution | `bool` | `false` | no |
| <a name="input_schedule_expression"></a> [schedule\_expression](#input\_schedule\_expression) | EventBridge schedule expression. Defaults to rate(previous\_days days) when null | `string` | `null` | no |
| <a name="input_sfn_child_execution_type"></a> [sfn\_child\_execution\_type](#input\_sfn\_child\_execution\_type) | Execution type of the Distributed Map child workflows. EXPRESS caps each date prefix at 5 minutes total regardless of compact\_lambda\_timeout, which a busy date on a large source cannot meet. STANDARD lifts that ceiling at Standard-workflow pricing | `string` | `"EXPRESS"` | no |
| <a name="input_sfn_max_concurrency"></a> [sfn\_max\_concurrency](#input\_sfn\_max\_concurrency) | Maximum concurrent child executions of the Distributed Map | `number` | `100` | no |
| <a name="input_sfn_tolerated_failure_count"></a> [sfn\_tolerated\_failure\_count](#input\_sfn\_tolerated\_failure\_count) | Number of failed date prefixes the Distributed Map tolerates before the whole execution fails. Unset keeps the Step Functions default of zero, where one failed date fails the run after other dates have already written their output | `number` | `null` | no |
| <a name="input_sfn_tolerated_failure_percentage"></a> [sfn\_tolerated\_failure\_percentage](#input\_sfn\_tolerated\_failure\_percentage) | Percentage (0-100) of failed date prefixes the Distributed Map tolerates before the whole execution fails. Unset keeps the Step Functions default of zero. If both count and percentage are set the run fails when either is exceeded | `number` | `null` | no |
| <a name="input_source_s3_uri"></a> [source\_s3\_uri](#input\_source\_s3\_uri) | S3 URI holding the small objects to compact, e.g. s3://my-bucket/raw/. Date prefixes are appended to it. A key prefix is required: the Lambda IAM policies are scoped to it | `string` | n/a | yes |
| <a name="input_standalone_lambda_memory_size"></a> [standalone\_lambda\_memory\_size](#input\_standalone\_lambda\_memory\_size) | Memory size in MB for the standalone compaction Lambda | `number` | `1024` | no |
| <a name="input_standalone_lambda_timeout"></a> [standalone\_lambda\_timeout](#input\_standalone\_lambda\_timeout) | Timeout in seconds for the standalone compaction Lambda | `number` | `900` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to all resources created by this module | `map(string)` | `{}` | no |
| <a name="input_target_s3_uri"></a> [target\_s3\_uri](#input\_target\_s3\_uri) | S3 URI where compacted objects are written, e.g. s3://my-bucket/compacted/. Date prefixes are appended to it | `string` | n/a | yes |
| <a name="input_target_sse_algorithm"></a> [target\_sse\_algorithm](#input\_target\_sse\_algorithm) | Server-side encryption requested on every object the functions write to the target bucket, the compacted output and the Distributed Map manifest. Null sends no encryption header and relies on the bucket default. Set AES256 when the target bucket policy denies uploads without an x-amz-server-side-encryption header: a bucket default encrypts the object but does not satisfy that condition | `string` | `null` | no |

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
