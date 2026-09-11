# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The Step Functions state machine, its IAM role and its execution log group are now created by [`terraform-aws-modules/step-functions/aws`](https://registry.terraform.io/modules/terraform-aws-modules/step-functions/aws) pinned to `5.1.1`, instead of hand-rolled `aws_sfn_state_machine` / `aws_iam_role` / `aws_cloudwatch_log_group` resources ([#5](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/5)). Section 5 of the FivexL module standardisation guide requires a verified registry module where one exists, and the Lambda functions in this module already follow that rule. The module's `service_integrations` and its `attach_cloudwatch_logs_policy` default now supply the `lambda:InvokeFunction`, `states:StartExecution`, X-Ray and vended-log-delivery statements; the two statements with no service integration (`s3:GetObject` on the prefix manifest and `states:DescribeExecution` / `states:StopExecution` on child executions) are passed as `policy_statements`. The set of granted actions and resources is unchanged
- **`aws` provider floor raised from `>= 6.0` to `>= 6.28`**, required by the step-functions module

### Upgrade notes

`moved` blocks carry the state machine, its IAM role and its log group into the
module, so none of the three is replaced and the role keeps its
`<name>-state-machine-role` name. One resource cannot be moved:
`aws_iam_role_policy.state_machine` is destroyed and the same permissions are
recreated as managed policies attached to the role, because that is how the
module models them. Expect a plan along these lines and no other drift:

- 1 destroy (`aws_iam_role_policy.state_machine`)
- 10 adds: 5 managed policies (`-lambda`, `-stepfunction`, `-xray`, `-inline`, `-logs`) and their 5 attachments. `-logs` is unconditional, because this module hardcodes `logging_configuration.level = "ALL"`
- in-place updates on the role (`force_detach_policies`, and the trust principal moving from `states.amazonaws.com` to the regional `states.<region>.amazonaws.com`) and on the state machine (a `Name` tag)

### Fixed

- Compaction is now idempotent. Both compact handlers wrote their merged output to a fixed path under `/tmp` in append mode and never removed it. Lambda reuses warm execution environments with `/tmp` intact, so re-running a date appended the day's data onto the previous run's file and uploaded an object with the content doubled, a retry after a timeout appended onto the partial file, and a long backlog filled the disk. The output is now a fresh temp file per run, truncated on open and removed afterwards, so re-running a date overwrites instead of appending ([#7](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/7))

### Security

- The state-machine role trust policy now requires `aws:SourceAccount` to be this account and `aws:SourceArn` to be the compaction state machine. The role carries `"*"`-scoped CloudWatch Logs delivery grants that AWS does not allow narrowing, so without the conditions any principal with `iam:PassRole` on the role and `states:CreateStateMachine` could attach it to their own state machine ([#7](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/7))

## [0.2.1] - 2026-09-09

### Changed

- Pin the nested `terraform-aws-modules/lambda/aws` module to exactly `8.8.0` instead of `~> 8.0`. Module sources are not covered by `.terraform.lock.hcl`, so a floating constraint re-resolved to the newest 8.x on every `terraform init`, and that module runs `package.py` at plan time. Bumps are now deliberate one-line diffs. No resource changes

## [0.2.0] - 2026-09-09

### Changed

- Lambda IAM policies now grant `s3:GetObject` and `s3:ListBucket` only on the key prefix of `source_s3_uri` instead of the whole source bucket. The handlers take the source URI from the invocation event, so IAM is what bounds them to the configured prefix ([#1](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/1))
- `source_s3_uri` must now include a key prefix (`s3://<bucket>/<prefix>`). A bare bucket URI is rejected at plan time because it would widen the grant back to the whole bucket

## [0.1.0] - 2026-09-07

### Added

- Initial conversion of [aws-samples/s3-small-object-compaction](https://github.com/aws-samples/s3-small-object-compaction) from AWS CDK to a Terraform module
